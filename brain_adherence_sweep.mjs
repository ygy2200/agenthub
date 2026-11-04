#!/usr/bin/env node
/**
 * AgentHub 采纳补扫钩子（R851）：下一次发言的开头，补判上一轮「注入的记忆有没有被用」。
 *
 * 为什么需要它：采纳回填挂在 Stop 上，而 Stop 不是所有宿主都发。本机实测同一个
 * Qoder 内核，qodercli 的会话一次跑 60 多次 Stop，Desktop 的会话（3 次用户发言）
 * 一次 Stop 都没有——于是 Desktop 里干活的那一家永远不交采纳账，`adopt_count`
 * 这条唯一的「真的被用上了」信号长期饿着，开工推送的排序也就拿不到反馈。
 *
 * 判定时点往后挪一拍：用户开口这句话时，上一轮早已写进 transcript，那段就是本轮
 * 该判的证据。触发频率与「即时回忆」相同（每句话一次），**不是** PostToolUse——
 * 一轮几十次工具调用每次都比对指纹才是纯浪费（R828 那条理由仍然成立，只是不能
 * 再拿它当「只能挂在 Stop」的理由）。
 *
 * 与 Stop 回填并存不冲突：本脚本按会话记水位线（已判定到 transcript 的哪个字节），
 * 每段证据只被读走一次；谁先跑到都读同一份水位线，重复触发就是空转。
 * 首轮没有「上一轮」可判，水位线从当前文件尾开始，不会把会话开头算成本轮产出。
 *
 * 延迟预算：判定进程 detached 拉起、证据经文件交接，钩子自己写完水位线立刻退出——
 * 用户在等这一句话的回答，不能让 python 的启动时间压在头上（R828 那条「记账脚本绝不
 * 能拦住用户」的纪律）。回归与实跑装置设 `AGENTHUB_SWEEP_SYNC=1` 走同步路径拿确定性。
 * 代价说清楚：detached 时水位线在交棒时就推进，判定进程若崩了这一段证据不重试
 * （下段照判）——少一笔账，不拦人。
 *
 * 纪律同其余钩子：永远 exit 0；Python 侧 5 秒超时（同步路径）；只读本地库；拿不到证据就静默。
 * 证据口径与 R849 一致：assistant 正文 + tool_use 名称与参数；thinking 与
 * tool_result 不算（前者会复述注入原文，后者是读进来的文件内容）。
 */
import { spawn, spawnSync } from 'node:child_process';
import {
  appendFileSync, closeSync, existsSync, fstatSync, mkdirSync, openSync,
  readFileSync, readSync, statSync, writeFileSync,
} from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';

const HOME = homedir();
const LOG = path.join(HOME, '.agenthub', 'logs', 'brain-adherence.log');
const CONFIG = path.join(HOME, '.agenthub', 'config.json');
const STATE = path.join(HOME, '.agenthub', 'state');
const SCRIPT = process.env.AGENTHUB_ADHERENCE_SCRIPT
  || path.join(HOME, '.agenthub', 'mcp_server', 'brain_adherence.py');
// 一次补扫最多读 1.2MB、喂 120K 字符：长会话里两轮之间能塞下整篇文档，
// 上限只在「窗口尾部」取值——越新的动作越能代表这一轮干了什么。
const MAX_BYTES = 1200000;
const MAX_TEXT = 120000;
const MAX_TOOL_INPUT = 2000;

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

/** 会话 id 可能带奇怪字符，落盘文件名只留安全字符并截断。 */
function safeTag(agent, session) {
  return `${agent || 'na'}-${session}`.replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 96);
}

/** 水位线文件：offset=已判定到的字节，seen_up_to=最后一次看到的文件尾。 */
function wmFile(agent, session) {
  return path.join(STATE, `adh-sweep-${safeTag(agent, session)}.json`);
}

function readWm(file) {
  try {
    const d = JSON.parse(readFileSync(file, 'utf8'));
    const off = Number(d.offset);
    return Number.isFinite(off) && off >= 0 ? d : null;
  } catch { return null; }
}

function writeWm(file, offset, seenUpTo, chars) {
  try {
    mkdirSync(STATE, { recursive: true });
    const old = readWm(file) || {};
    writeFileSync(file, JSON.stringify({
      offset,
      seen_up_to: seenUpTo === undefined ? Number(old.seen_up_to) || 0 : seenUpTo,
      last_chars: chars === undefined ? Number(old.last_chars) || 0 : chars,
      ts: new Date().toISOString(),
    }), 'utf8');
  } catch { /* 水位线写不了就下次重扫，宁重复不漏 */ }
}

/** 读 [start, end) 区间；超过 maxBytes 时只取尾部（新证据优先），返回真正的读取起点。 */
function readRange(file, start, end, maxBytes) {
  let fd = -1;
  try {
    const len = end - start;
    if (len <= 0) return { text: '', from: start, taken: 0 };
    const take = Math.min(len, maxBytes);
    const from = start + (len - take);
    fd = openSync(file, 'r');
    const buf = Buffer.alloc(take);
    const got = readSync(fd, buf, 0, take, from);
    return { text: buf.toString('utf8', 0, got), from, taken: got };
  } catch (e) {
    logLine(`sweep 读取失败 ${String((e && e.message) || e).slice(0, 120)}`);
    return { text: '', from: start, taken: 0 };
  } finally {
    if (fd >= 0) { try { closeSync(fd); } catch { /* ignore */ } }
  }
}

/** 一条消息里能当证据的片段（R873：含 workbuddy 的 input_text/output_text）。
 *
 * 只认 `text` 的那版在 workbuddy 上一次都抽不到：它的正文 part 叫 output_text，
 * 工具调用是独立 `function_call` 事件。证据饿着时「未观测」会冒充「记忆没人用」。
 */
function evidenceFromContent(content) {
  const out = [];
  if (typeof content === 'string') { out.push(content); return out; }
  if (!Array.isArray(content)) return out;
  for (const part of content) {
    if (typeof part === 'string') { out.push(part); continue; }
    if (!part || typeof part !== 'object') continue;
    const type = part.type || '';
    if ((type === 'text' || type === 'input_text' || type === 'output_text')
      && typeof part.text === 'string') out.push(part.text);
    else if (type === 'tool_use') {
      let blob = '';
      try { blob = JSON.stringify(part.input || {}).slice(0, MAX_TOOL_INPUT); } catch { blob = ''; }
      out.push(`${part.name || ''} ${blob}`);
    }
  }
  return out;
}

/** 一条事件折算成证据。function_call（workbuddy）没有 role，靠事件类型认。 */
function evidenceFromEvent(ev) {
  const out = [];
  const msg = (ev && ev.message && typeof ev.message === 'object') ? ev.message : ev;
  const role = (msg && msg.role) || (ev && ev.role) || '';
  const etype = String((ev && ev.type) || '');
  if (role === 'assistant') out.push(...evidenceFromContent(msg.content));
  if (etype === 'function_call') {
    let blob = '';
    const args = ev.arguments;
    if (typeof args === 'string') blob = args.slice(0, MAX_TOOL_INPUT);
    else {
      try { blob = JSON.stringify(args || {}).slice(0, MAX_TOOL_INPUT); } catch { blob = ''; }
    }
    out.push(`${ev.name || ''} ${blob}`);
  }
  return out;
}

/** 真人开口判定（轮边界）。R873：workbuddy 的提问 part 是 input_text。 */
function isRealPrompt(content) {
  if (typeof content === 'string') return !!content.trim();
  if (!Array.isArray(content)) return false;
  return content.some((p) => p && typeof p === 'object'
    && (p.type === 'text' || p.type === 'input_text' || p.type === 'output_text')
    && typeof p.text === 'string' && p.text.trim());
}

/** 事件时间戳 → 本地 ISO（DB 的 ts 是本地时间，比较必须同口径）。
 *
 * Qoder 给 `2026-11-04T08:40:00.783Z`，workbuddy 给 epoch 毫秒；无时区的字符串按本地算。
 */
function isoLocal(v) {
  if (v === undefined || v === null || v === '') return '';
  let d;
  if (typeof v === 'number') d = new Date(v < 1e12 ? v * 1000 : v);
  else {
    const s = String(v);
    d = new Date(s);
    if (Number.isNaN(d.getTime())) {
      const m = s.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/);
      return m ? `${m[1]}T${m[2]}` : '';
    }
  }
  if (Number.isNaN(d.getTime())) return '';
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
    + `T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/**
 * 首见会话时取「最后一轮」的证据与开口时间（R873）。
 *
 * 为什么需要：第一句话的注入发生在 transcript 落盘之前——SessionStart/首句的
 * UserPromptSubmit 拿不到文件，补扫第一次真正看见会话时水位线直接跳到文件尾，
 * 于是**第一轮的注入永远没人判**（本机实测 a3aa5b2a：16:39 注入 3 条，16:59 首见
 * 跳尾，之后整局都停在未观测）。这里在首见时把尾部的最后一轮判掉，并用开口时间做
 * 下界：只认「这一轮开始之后」的注入，历史会话的旧账不许借尸还魂（R851 那条纪律）。
 * 尾部窗口没兜住轮首（>1.2MB 的巨轮）时不判——宁少一笔，不拿半段冒充整轮。
 */
function firstSightWindow(raw) {
  const lines = raw.split('\n');
  if (lines.length && lines[0].trim() && lines[0].trim()[0] !== '{') lines.shift();
  const current = [];
  let promptTs = '';
  let seenPrompt = false;
  let events = 0;
  let lastTurn = null;
  for (const line of lines) {
    const t = line.trim();
    if (!t || t[0] !== '{') continue;
    let ev;
    try { ev = JSON.parse(t); } catch { continue; }
    const msg = (ev && ev.message) || ev;
    const role = (msg && msg.role) || (ev && ev.role) || '';
    if (role === 'user') {
      if (isRealPrompt(msg.content)) {
        // 有些宿主先把这一句写进 transcript 再触发钩子：此时最后一轮还没有产出，
        // 退回上一轮（它才是「这句话之前的完整一轮」），开口时间跟着退。
        if (current.length) lastTurn = { text: current.join('\n'), ts: promptTs };
        current.length = 0;
        seenPrompt = true;
        promptTs = isoLocal((ev && ev.timestamp) || (msg && msg.timestamp));
      }
      continue;
    }
    const pieces = evidenceFromEvent(ev);
    if (!pieces.length) continue;
    for (const piece of pieces) {
      if (piece && piece.trim()) current.push(piece);
    }
    events += 1;
  }
  const chosen = current.length ? { text: current.join('\n'), ts: promptTs } : lastTurn;
  if (!chosen) return { text: '', promptTs: '', seenPrompt, events };
  return { text: chosen.text, promptTs: chosen.ts, seenPrompt, events };
}

/**
 * 从增量里取 assistant 证据，并算出「消费到哪个字节」。
 * 只消费到最后一个完整行——transcript 正在被写，最后一行可能是半截 JSON。
 * `readFrom` 是本次读取在文件里的真实起点（窗口被截尾时它大于水位线，跳过的
 * 那段是超出预算的旧证据，按「新证据优先」的取舍主动放弃，不再回头）。
 */
function sweepWindow(raw, readFrom) {
  const lastNl = raw.lastIndexOf('\n');
  if (lastNl < 0) return { text: '', nextOffset: readFrom, events: 0 };
  const consumed = raw.slice(0, lastNl + 1);
  const pieces = [];
  let events = 0;
  for (const line of consumed.split('\n')) {
    const t = line.trim();
    if (!t || t[0] !== '{') continue;
    let ev;
    try { ev = JSON.parse(t); } catch { continue; }
    const msg = (ev && ev.message) || ev;
    const role = (msg && msg.role) || (ev && ev.role) || '';
    if (role === 'user') continue;           // user（含注入上下文）不算产出
    // R873：function_call（workbuddy）没有 role，靠事件类型认；reasoning/*_result 返回空。
    const evPieces = evidenceFromEvent(ev);
    if (!evPieces.length) continue;
    for (const piece of evPieces) {
      if (piece && piece.trim()) pieces.push(piece);
    }
    events += 1;
  }
  return { text: pieces.join('\n'), nextOffset: readFrom + Buffer.byteLength(consumed, 'utf8'),
           events };
}

try {
  const raw = await readStdin();
  let ev = {};
  try { ev = raw.trim() ? JSON.parse(raw) : {}; } catch { ev = {}; }
  const event = String(ev.hook_event_name || ev.hookEventName || ev.event || 'UserPromptSubmit');
  const en = event.toLowerCase().replace(/[_-]/g, '');
  if (en !== 'userpromptsubmit' && en !== 'userpromptsubmitted') process.exit(0);

  const session = String(ev.session_id || ev.sessionId || ev.conversation_id || '');
  if (!session) { logLine('skip sweep: 无 session'); process.exit(0); }
  const tp = String(ev.transcript_path || ev.transcriptPath || '');
  if (!tp || !existsSync(tp)) {
    logLine(`skip sweep: 无 transcript session=${session.slice(0, 24)}`);
    process.exit(0);
  }
  if (!existsSync(SCRIPT)) { logLine(`skip sweep: ${SCRIPT} 不存在`); process.exit(0); }

  const ai = process.argv.slice(2);
  const aiIdx = ai.indexOf('--agent');
  const agent = (aiIdx >= 0 ? ai[aiIdx + 1] : '') || ev.agent || process.env.AGENTHUB_AGENT || '';
  const wm = wmFile(agent, session);
  const size = statSync(tp).size;
  const prev = readWm(wm);
  let text = '';
  let nextOffset = size;
  let src = 'sweep';
  let since = '';
  let bytesTaken = 0;
  let events = 0;

  if (!prev) {
    // R873 首见：第一轮的注入发生在 transcript 落盘之前，第一次真正看见会话时
    // 水位线直接跳到尾，会把这一轮永久留在「未观测」（实测 a3aa5b2a：16:39 注入
    // 3 条，16:59 首见跳尾，整局没人判）。改成把尾部最后一轮判掉；`--since` 用
    // 那一轮开口时间做下界，历史会话的旧账不许借尸还魂（R851 那条纪律）。
    const tail = readRange(tp, Math.max(0, size - MAX_BYTES), size, MAX_BYTES);
    const win = firstSightWindow(tail.text);
    // 水位线只吃到最后一个完整行：文件可能正在写，半截行留给下一次。
    const lastNl = tail.text.lastIndexOf('\n');
    nextOffset = lastNl >= 0
      ? tail.from + Buffer.byteLength(tail.text.slice(0, lastNl + 1), 'utf8')
      : size;
    text = (win.text || '').trim();
    bytesTaken = tail.taken;
    events = win.events;
    if (!win.seenPrompt || !text || !win.promptTs) {
      writeWm(wm, nextOffset, size);
      logLine(`skip sweep: 首见无可判轮 session=${session.slice(0, 24)}`
        + ` prompt=${win.seenPrompt ? 'y' : 'n'} ev=${win.events}`);
      process.exit(0);
    }
    src = 'firstsight';
    since = win.promptTs;
  } else {
    if (size <= prev.offset) {
      // 文件被换/截断（新 session 复用同名？）：重置到尾，不拿旧内容冒充新产出。
      if (size < prev.offset) writeWm(wm, size, size);
      process.exit(0);
    }
    const chunk = readRange(tp, prev.offset, size, MAX_BYTES);
    const win = sweepWindow(chunk.text, chunk.from);
    text = (win.text || '').trim();
    bytesTaken = chunk.taken;
    events = win.events;
    nextOffset = Math.max(prev.offset, win.nextOffset);
    if (!text) {
      writeWm(wm, nextOffset, size);
      logLine(`skip sweep: 增量无产出 session=${session.slice(0, 24)} bytes=${chunk.taken}`);
      process.exit(0);
    }
  }
  const body = text.length > MAX_TEXT ? text.slice(-MAX_TEXT) : text;

  const args = [SCRIPT, '--agent', agent, '--session', session, '--src', src];
  if (since) args.push('--since', since);
  let rootArg = process.env.AGENTHUB_ROOT || '';
  if (!rootArg && existsSync(CONFIG)) {
    try { rootArg = String(JSON.parse(readFileSync(CONFIG, 'utf8')).root || ''); } catch { /* ignore */ }
  }
  if (rootArg) args.push('--root', rootArg);

  // 传送带用文件而不是 stdin/命令行：钩子要立刻交还这一拍（用户在等这一句话），而一轮
  // 证据动辄几万字节，命令行也装不下。写完文件才推进水位线——写不进就原地不动，
  // 下次发言同一批证据重来。
  const evFile = path.join(STATE, `adh-sweep-${safeTag(agent, session)}.evidence.txt`);
  try {
    mkdirSync(STATE, { recursive: true });
    writeFileSync(evFile, body, 'utf8');
  } catch (e) {
    logLine(`sweep 证据落盘失败，本轮不判 session=${session.slice(0, 24)}`
      + ` ${String((e && e.message) || e).slice(0, 100)}`);
    process.exit(0);
  }
  args.push('--text-file', evFile);
  const py = pickPython();
  const sync = process.env.AGENTHUB_SWEEP_SYNC === '1';
  try {
    if (sync) {
      // 装置/回归要确定性：同步等 python 跑完，rc 非 0 就不推进水位线。
      const r = spawnSync(py, args, {
        encoding: 'utf8', timeout: 5000,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
        maxBuffer: 8 * 1024 * 1024,
      });
      if (r.status !== 0) {
        logLine(`sweep rc=${r.status} err=${String(r.stderr || r.error || '').slice(0, 160)}`);
        process.exit(0);
      }
    } else {
      // 生产口径：detached 拉起判定进程，自己立刻退出。记账晚几十毫秒到几秒到账都行，
      // 把 python 的启动时间压在用户这一拍的延迟里不行（R828 那条纪律的原话）。
      const child = spawn(py, args, {
        detached: true, stdio: 'ignore',
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
      });
      child.on('error', (e) => logLine(`sweep spawn 失败 ${String(e && e.message || e).slice(0, 120)}`));
      child.unref();
    }
  } catch (e) {
    logLine(`sweep spawn 异常 ${String((e && e.message) || e).slice(0, 120)}`);
    process.exit(0);
  }
  writeWm(wm, nextOffset, size, body.length);
  logLine(`ok sweep session=${session.slice(0, 24)} src=${src} chars=${body.length}`
    + ` ev=${events} bytes=${bytesTaken}${since ? ` since=${since}` : ''}`
    + `${sync ? ' sync' : ' detached'}`);
} catch (e) {
  logLine(`sweep error ${String((e && e.message) || e).slice(0, 160)}`);
  process.exit(0);
}
