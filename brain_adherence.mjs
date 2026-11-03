#!/usr/bin/env node
/**
 * AgentHub 采纳回填钩子（R828，证据口径改造 R849）：一轮结束时把这一轮的产出喂回大脑，
 * 标记「这次注入的记忆里，哪几条真的被用到了」。
 *
 * 为什么挂在 Stop 而不是 PostToolUse：一轮里的工具调用可能有几十次，每次都比对一遍
 * 指纹是纯浪费；一轮结束时产出的东西才是「结果」，那时候判一次最准也最省。
 *
 * R849 改的是「喂什么进去判」。上一版按「事件里有没有文本字段」降级，实测线上
 * Qoder 的 Stop 只给收尾那一句话（日志里 chars=19、1906 这种量级），而记忆指纹抽的是
 * `hub_graph`、`build.bat`、`--add-data`、文件路径——这些东西活在**本轮的工具调用参数**里，
 * 不在收尾语里。于是 177 笔账只标出 9 条 used（5.5%），看着像「AI 根本不用注入的记忆」，
 * 实际是判官饿着。同口径对照真实会话：只看收尾文本命中 2/14，补上本轮工具调用命中 9/14。
 * 所以现在的取证顺序是：transcript 里**本轮**的 assistant 正文 + tool_use 参数为主，
 * 事件自带的文本为辅（各家载荷不一样，谁有用谁的）。
 *
 * 两条纪律没变：
 *   · 永远 exit 0，5 秒超时，不写任何用户配置——记账脚本绝不能拦住用户；
 *   · 只取本轮，不回看整段会话：整会话比对会把「十轮前碰巧用过同一个文件名」算成采纳。
 *
 * `thinking` 段刻意不算证据：注入原文就挂在同一条消息的系统提示里，模型复述一遍
 * 不等于照着干活，算进去只会把采纳率做成好看的假数。
 */
import { spawnSync } from 'node:child_process';
import { closeSync, fstatSync, openSync, readSync, readFileSync, existsSync, mkdirSync, appendFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';

const HOME = homedir();
const LOG = path.join(HOME, '.agenthub', 'logs', 'brain-adherence.log');
const CONFIG = path.join(HOME, '.agenthub', 'config.json');
const SCRIPT = process.env.AGENTHUB_ADHERENCE_SCRIPT
  || path.join(HOME, '.agenthub', 'mcp_server', 'brain_adherence.py');
// 尾部窗口。一轮重型工作（几十次 Edit/Write）的 JSONL 能写到上百万字，400KB 会把本轮
// 开头切掉；读到 1.2MB 仍然只是一次小 IO，比整读 10MB 的会话文件便宜两个数量级。
const MAX_BYTES = 1200000;
const MAX_TEXT = 120000;
const MAX_TOOL_INPUT = 2000;   // 单条工具参数截断，防止一次 Write 塞进整份文件正文

function logLine(msg) {
  try {
    mkdirSync(path.dirname(LOG), { recursive: true });
    appendFileSync(LOG, `${new Date().toISOString()}  ${msg}\n`, 'utf8');
  } catch { /* ignore */ }
}

async function readStdin() {
  const chunks = [];
  try {
    for await (const c of process.stdin) chunks.push(c);
  } catch { /* ignore */ }
  return Buffer.concat(chunks).toString('utf8');
}

function pickPython() {
  const env = process.env.AGENTHUB_PYTHON;
  if (env && existsSync(env)) return env;
  for (const p of ['D:/python311/python.exe', 'D:/Python311/python.exe']) {
    if (existsSync(p)) return p;
  }
  return 'python';
}

/** 只读文件尾部（定位读，不整读会话文件）。 */
function readTail(file, maxBytes) {
  let fd = -1;
  try {
    if (!file || !existsSync(file)) return '';
    fd = openSync(file, 'r');
    const size = fstatSync(fd).size;
    const start = Math.max(0, size - maxBytes);
    const len = size - start;
    if (len <= 0) return '';
    const buf = Buffer.alloc(len);
    readSync(fd, buf, 0, len, start);
    return buf.toString('utf8');
  } catch (e) {
    logLine(`尾部读取失败 ${String((e && e.message) || e).slice(0, 120)}`);
    return '';
  } finally {
    if (fd >= 0) { try { closeSync(fd); } catch { /* ignore */ } }
  }
}

/** 一条消息里能当证据的片段：正文文本 + 工具调用（名称与参数）。 */
function evidenceFromContent(content) {
  const out = [];
  if (typeof content === 'string') { out.push(content); return out; }
  if (!Array.isArray(content)) return out;
  for (const part of content) {
    if (typeof part === 'string') { out.push(part); continue; }
    if (!part || typeof part !== 'object') continue;
    const type = part.type || '';
    if (type === 'text' && typeof part.text === 'string') out.push(part.text);
    else if (type === 'tool_use') {
      let blob = '';
      try { blob = JSON.stringify(part.input || {}).slice(0, MAX_TOOL_INPUT); } catch { blob = ''; }
      out.push(`${part.name || ''} ${blob}`);
    }
    // thinking / tool_result 不算证据：前者会复述注入原文，后者是读进来的文件内容。
  }
  return out;
}

/** 这条 user 消息是不是「真人开口」——轮边界就看它（tool_result 与注入的上下文不算）。 */
function isRealPrompt(content) {
  if (typeof content === 'string') return !!content.trim();
  if (!Array.isArray(content)) return false;
  return content.some((p) => p && typeof p === 'object' && p.type === 'text'
    && typeof p.text === 'string' && p.text.trim());
}

/**
 * 取「本轮」的证据：从尾部按真人开口切轮，取最后一段里的 assistant 片段。
 * 尾部窗口没兜住轮开头时（超长轮）退化成「窗口内所有 assistant 片段」，宁多不漏本轮。
 */
function turnFromTranscript(file) {
  const raw = readTail(file, MAX_BYTES);
  if (!raw) return { text: '', events: 0, reset: false };
  const lines = raw.split('\n');
  if (lines.length && lines[0].trim() && lines[0].trim()[0] !== '{') lines.shift();
  const current = [];
  const fallback = [];
  let events = 0;
  let reset = false;
  for (const line of lines) {
    const t = line.trim();
    if (!t || t[0] !== '{') continue;
    let ev;
    try { ev = JSON.parse(t); } catch { continue; }
    const msg = (ev && ev.message) || ev;
    const role = (msg && msg.role) || (ev && ev.role) || '';
    if (role === 'user') {
      if (isRealPrompt(msg.content)) { current.length = 0; reset = true; }
      continue;
    }
    if (role !== 'assistant') continue;
    for (const piece of evidenceFromContent(msg.content)) {
      if (piece && piece.trim()) { current.push(piece); fallback.push(piece); }
    }
    events += 1;
  }
  const picked = current.length ? current : fallback.slice(-8);
  return { text: picked.join('\n'), events, reset };
}

/** 事件直接给的文本（各家字段名不统一，逐个试）。 */
function directText(ev) {
  for (const k of ['last_assistant_message', 'lastAssistantMessage', 'response',
    'assistant_message', 'text', 'message', 'output']) {
    const v = ev?.[k];
    if (typeof v === 'string' && v.trim()) return v.trim();
    if (v && typeof v === 'object') {
      const c = v.content ?? v.text;
      if (typeof c === 'string' && c.trim()) return c.trim();
    }
  }
  return '';
}

try {
  const raw = await readStdin();
  let ev = {};
  try { ev = raw.trim() ? JSON.parse(raw) : {}; } catch { ev = {}; }
  const event = String(ev.hook_event_name || ev.hookEventName || ev.event || 'Stop');
  const en = event.toLowerCase().replace(/[_-]/g, '');
  if (en !== 'stop' && en !== 'agentstop' && en !== 'subagentstop') process.exit(0);

  const session = String(ev.session_id || ev.sessionId || ev.conversation_id || '');
  if (!session) { logLine('skip: 无 session'); process.exit(0); }

  const turn = turnFromTranscript(String(ev.transcript_path || ev.transcriptPath || ''));
  const direct = directText(ev);
  // 本轮正文通常已经包含收尾那句，重复喂只会让匹配多算一遍；真给了额外内容才并入。
  let parts = [];
  if (turn.text) parts.push(turn.text);
  if (direct && !turn.text.includes(direct)) parts.push(direct);
  const text = parts.join('\n').trim();
  const src = turn.text && direct ? 'transcript+direct'
    : (turn.text ? 'transcript' : (direct ? 'direct' : ''));
  if (!text) { logLine(`skip: 取不到产出 session=${session.slice(0, 24)} tp=${turn.text ? 'y' : 'n'}`); process.exit(0); }
  const body = text.length > MAX_TEXT ? text.slice(-MAX_TEXT) : text;
  if (!existsSync(SCRIPT)) { logLine(`skip: ${SCRIPT} 不存在`); process.exit(0); }

  const ai = process.argv.slice(2);
  const aiIdx = ai.indexOf('--agent');
  const agent = (aiIdx >= 0 ? ai[aiIdx + 1] : '')
    || ev.agent || process.env.AGENTHUB_AGENT || '';

  // 文本走 stdin，不进命令行：Windows 的命令行上限是 32767 字符，而一轮证据动辄几万字符，
  // 用 --text 传超长轮会被 CreateProcess 直接拒掉——记不上账还是小事，整次判定静默蒸发。
  const args = [SCRIPT, '--agent', agent, '--session', session, '--src', src];
  let rootArg = process.env.AGENTHUB_ROOT || '';
  if (!rootArg && existsSync(CONFIG)) {
    try { rootArg = String(JSON.parse(readFileSync(CONFIG, 'utf8')).root || ''); } catch { /* ignore */ }
  }
  if (rootArg) args.push('--root', rootArg);

  const r = spawnSync(pickPython(), args, {
    input: body,
    encoding: 'utf8', timeout: 5000,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    maxBuffer: 8 * 1024 * 1024,
  });
  if (r.status !== 0) {
    logLine(`rc=${r.status} err=${String(r.stderr || r.error || '').slice(0, 160)}`);
  } else {
    logLine(`ok session=${session.slice(0, 24)} src=${src} chars=${body.length}`
      + ` turn_ev=${turn.events}${turn.reset ? '' : '+'}`);
  }
} catch (e) {
  logLine(`error ${String((e && e.message) || e).slice(0, 160)}`);
  process.exit(0);
}
