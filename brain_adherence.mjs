#!/usr/bin/env node
/**
 * AgentHub 采纳回填钩子（R828）：一轮结束时把这一轮的产出喂回大脑，
 * 标记「这次注入的记忆里，哪几条真的被用到了」。
 *
 * 为什么挂在 Stop 而不是 PostToolUse：一轮里的工具调用可能有几十次，每次都比对一遍
 * 指纹是纯浪费；一轮结束时产出的东西才是「结果」，那时候判一次最准也最省。
 *
 * 拿产出的三级降级（各家 Event 载荷不一样，不能假设都给了全文）：
 *   ① 事件里直接有文本字段（last_assistant_message / response / text / message）→ 用它；
 *   ② 有 transcript_path（Claude 系）→ 读 JSONL 尾部，取最后几条 assistant 消息；
 *   ③ 都没有 → 静默退出（fail-open，绝不因为拿不到文本而卡住会话）。
 *
 * 纪律：永远 exit 0；5 秒超时；不写任何用户配置。
 */
import { spawnSync } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, readFileSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';

const HOME = homedir();
const LOG = path.join(HOME, '.agenthub', 'logs', 'brain-adherence.log');
const CONFIG = path.join(HOME, '.agenthub', 'config.json');
const SCRIPT = process.env.AGENTHUB_ADHERENCE_SCRIPT
  || path.join(HOME, '.agenthub', 'mcp_server', 'brain_adherence.py');
const MAX_BYTES = 400000;        // 只读 transcript 尾部，避免几十 MB 的会话整读
const MAX_TEXT = 40000;

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

/** 从 transcript JSONL 尾部捞 assistant 文本。 */
function textFromTranscript(file) {
  try {
    if (!file || !existsSync(file)) return '';
    const size = statSync(file).size;
    const start = Math.max(0, size - MAX_BYTES);
    const fd = readFileSync(file);
    const buf = fd.subarray(start);
    const lines = buf.toString('utf8').split('\n');
    const out = [];
    for (const line of lines) {
      const t = line.trim();
      if (!t || t[0] !== '{') continue;
      let ev;
      try { ev = JSON.parse(t); } catch { continue; }
      const role = ev?.message?.role || ev?.role || '';
      if (role !== 'assistant') continue;
      const content = ev?.message?.content ?? ev?.content;
      if (typeof content === 'string') { out.push(content); continue; }
      if (Array.isArray(content)) {
        for (const part of content) {
          if (typeof part === 'string') out.push(part);
          else if (part && typeof part.text === 'string') out.push(part.text);
          else if (part && part.type === 'tool_use') {
            out.push(`${part.name || ''} ${JSON.stringify(part.input || {}).slice(0, 2000)}`);
          }
        }
      }
    }
    // 只要最后 8 条 assistant 内容就够了——指纹匹配不需要整段会话
    return out.slice(-8).join('\n').slice(-MAX_TEXT);
  } catch (e) {
    logLine(`transcript 读取失败 ${String(e && e.message || e).slice(0, 120)}`);
    return '';
  }
}

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

  let text = directText(ev);
  if (!text) text = textFromTranscript(String(ev.transcript_path || ev.transcriptPath || ''));
  if (!text) { logLine(`skip: 取不到产出 session=${session.slice(0, 24)}`); process.exit(0); }
  if (text.length > MAX_TEXT) text = text.slice(-MAX_TEXT);
  if (!existsSync(SCRIPT)) { logLine(`skip: ${SCRIPT} 不存在`); process.exit(0); }

  const ai = process.argv.slice(2);
  const aiIdx = ai.indexOf('--agent');
  const agent = (aiIdx >= 0 ? ai[aiIdx + 1] : '')
    || ev.agent || process.env.AGENTHUB_AGENT || '';

  const args = [SCRIPT, '--agent', agent, '--session', session, '--text', text];
  let rootArg = process.env.AGENTHUB_ROOT || '';
  if (!rootArg && existsSync(CONFIG)) {
    try { rootArg = String(JSON.parse(readFileSync(CONFIG, 'utf8')).root || ''); } catch { /* ignore */ }
  }
  if (rootArg) args.push('--root', rootArg);

  const r = spawnSync(pickPython(), args, {
    encoding: 'utf8', timeout: 5000,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    maxBuffer: 8 * 1024 * 1024,
  });
  if (r.status !== 0) {
    logLine(`rc=${r.status} err=${String(r.stderr || '').slice(0, 160)}`);
  } else {
    logLine(`ok session=${session.slice(0, 24)} chars=${text.length}`);
  }
} catch (e) {
  logLine(`error ${String(e && e.message || e).slice(0, 160)}`);
  process.exit(0);
}
