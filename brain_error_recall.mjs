#!/usr/bin/env node
/**
 * AgentHub 报错即时回忆钩子（R833）：工具失败那一刻，把大脑里同签名的旧坑送到模型眼前。
 *
 * 病根：简报解决「开局」，按句回忆解决「用户说到某件事」，但模型真正需要历史结论的
 * 时刻是**它自己撞墙**那一刻。这时候大脑里那句「上次这条报错是这么修的」值一句提醒——
 * 也正是「不用人提醒就会用记忆」的落点。
 *
 * 事件兼容：首选 PostToolUseFailure（Claude/Qoder 契约），但并非各家都有——所以
 * PostToolUse 也接，只要 tool_response 里带错误标记（isError/errorMessage/stderr）。
 * 两条路都拿不到错误文本就静默退出，绝不猜。
 *
 * 三条硬约束（都在 python 侧 brain_error_recall.py 落实现，这里只做形状判断）：
 *   · 大脑自己的工具（hub_* / agenthub）失败不注：让大脑回忆自己 = 自我反馈循环；
 *   · 同会话同签名只说一次：重试循环每次失败都注一遍纯属刷屏；
 *   · 没把握不开口：注一段不相关的「上次的修法」比闭嘴更坏。
 * 纪律：永远 exit 0；5 秒超时；不写任何用户配置。
 */
import { spawnSync } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';

const HOME = homedir();
const LOG = path.join(HOME, '.workbuddy', 'logs', 'brain-error-recall.log');
const CONFIG = path.join(HOME, '.agenthub', 'config.json');
const SCRIPT = process.env.AGENTHUB_ERROR_SCRIPT
  || path.join(HOME, '.agenthub', 'mcp_server', 'brain_error_recall.py');
const MAX_CHARS = Math.max(200, Number(process.env.AGENTHUB_ERROR_MAX_CHARS) || 700);
const MIN_ERR_CHARS = 12;

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

function asText(v, depth = 0) {
  if (v == null || depth > 3) return '';
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  if (Array.isArray(v)) return v.map(x => asText(x, depth + 1)).filter(Boolean).join('\n');
  if (typeof v === 'object') {
    for (const k of ['errorMessage', 'error_message', 'message', 'text', 'stderr',
      'error', 'content', 'output', 'reason']) {
      if (v[k] != null) {
        const t = asText(v[k], depth + 1);
        if (t.trim()) return t;
      }
    }
    return JSON.stringify(v).slice(0, 4000);
  }
  return '';
}

/** 从各家事件载荷里捞出「这次失败到底是什么」。拿不到就返回空串（静默）。 */
let rejectedJson = false;   // 本轮是否因为「正文其实是事件 JSON dump」而拒过一条
function errorText(ev) {
  for (const k of ['error', 'errorMessage', 'error_message', 'failure',
    'tool_error', 'stderr', 'message']) {
    const t = asText(ev?.[k]);
    if (t.trim().length >= MIN_ERR_CHARS) return t.trim();
  }
  const resp = ev?.tool_response ?? ev?.toolResult ?? ev?.result;
  const isErr = resp?.isError ?? resp?.is_error ?? resp?.error ?? resp?.ok === false;
  const t = asText(resp);
  if (isErr != null && isErr !== false && t.trim().length >= MIN_ERR_CHARS) {
    // B045 的成因在这里现形（2026-11-04 采集面实锤）：`isError:true` 而 stderr/stdout
    // 全空时，`asText` 一路退到 `JSON.stringify(v)`，于是**整个事件 JSON 变成了「报错正文」**——
    // 钩子把 `{"isError":true,"stdout":"","stderr":""}` 递给召回，签名抽成 `iserror`，
    // 主人看到的欠账提示是「刚撞的：{"isError":true…}」。这不是报错，是挽具自己造的假报错。
    // 所以：一段 JSON dump 不算报错正文，交回上层走采集轨道（只采不注）。
    if (/^\s*[{[]/.test(t)) {
      try { JSON.parse(t); rejectedJson = true; logLine(`reject: 事件 JSON 被当报错正文 tool=${String(ev.tool_name || '').slice(0, 24)}`); return ''; } catch { /* 解析不了就不是 dump，继续 */ }
    }
    return t.trim();
  }
  // R971：命令整体 exit 0、但 stdout 里带 Python traceback（典型：`;` 串联把非零
  // 退出码吃掉）。宿主不会标 isError，旧逻辑整段静默跳过——大脑明明登记过同类修法
  // （R970 真库实测 error_recall 能命中 #20/#29），却卡在最后一百米递不出来。
  // 只认**结构化** traceback，不认普通 "Error"/"ERROR" 字样，防误报灌上下文。
  const out = asText(resp?.stdout ?? resp?.output ?? resp?.content ?? resp?.text ?? resp ?? '');
  const tb = extractTraceback(out);
  if (tb) {
    logLine('traceback_from_stdout: isError 缺失但检出结构化 traceback');
    return tb;
  }
  // R986：Node 形状（Python 之外唯一在本机 Bash 里会出现的运行时异常）
  const ne = extractNodeError(out);
  if (ne) {
    logLine('node_error_from_stdout: isError 缺失但检出 Node 异常行');
    return ne;
  }
  return '';
}

/** R971 纯函数：只从 stdout 认 Python 结构化 traceback，返回「头 + 最后异常行」。
 *  没有 Traceback 头、或找不到 `XxxError: ...` 异常行 → 空串（宁可漏也不误报）。
 *  只回传这两行，别把整段 File "..." 栈帧和 stdout 文件名喂给签名抽取（R865 口径）。 */
function extractTraceback(s) {
  if (typeof s !== 'string' || !s.includes('Traceback (most recent call last):')) return '';
  const lines = s.split(/\r?\n/);
  const start = lines.findIndex(l => l.includes('Traceback (most recent call last):'));
  let last = '';
  for (let i = start + 1; i < lines.length; i++) {
    if (/^\s*[A-Za-z_][\w.]*(Error|Exception)\s*:\s*\S/.test(lines[i])) last = lines[i].trim();
  }
  if (!last) return '';
  const out = 'Traceback (most recent call last):\n' + last;
  return out.length >= MIN_ERR_CHARS ? out : '';
}

/** R986（B119）：只从 stdout 认 Node 未捕获异常，返回错误行本身。
 *  为什么需要：`node -e "throw …" | head -3; echo done` 这类串联/管道把非零退出码吃掉，
 *  宿主不标 isError，而 R971 只认 Python 的 traceback 头——Node 形状整段静默跳过。
 *  真库旁证：近 14 天 24 条 exception_line 的 head 全是 python 调用，一条 Node 都没有。
 *  双条件防误报：① `XxxError: …` 行；② 前 6 行内有只含 caret 的定位行，或后 4 行内有 at 栈帧。
 *  只回传错误行：不带 [eval]:1 / 文件路径 / 栈（签名要稳定，R865 口径）。 */
function extractNodeError(s) {
  if (typeof s !== 'string' || !s) return '';
  const lines = s.split(/\r?\n/);
  let best = '';
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    // 前缀可选：Node 的 `new Error()` 打印的就是裸 `Error: …`（首版要求带前缀，漏了最常见那种）
    if (!/^(?:[A-Za-z_$][A-Za-z0-9_$.]*)?(?:Error|Exception):\s*\S/.test(line)) continue;
    let shaped = false;
    for (let k = Math.max(0, i - 6); k < i; k++) {
      // 整行只有 caret 才算定位行；`[^a]` 这类字符类里的 ^ 不算
      if (/^\s*\^+\s*$/.test(lines[k])) { shaped = true; break; }
    }
    if (!shaped) {
      for (let k = i + 1; k <= Math.min(lines.length - 1, i + 4); k++) {
        if (/^\s+at\s+\S/.test(lines[k])) { shaped = true; break; }
      }
    }
    if (shaped) best = line;
  }
  return best.length >= MIN_ERR_CHARS ? best : '';
}

function resolveRoot(ev) {
  const cwd = ev.cwd || ev.workingDirectory || ev.cwdPath || process.cwd();
  let rootArg = process.env.AGENTHUB_ROOT || '';
  if (!rootArg && existsSync(CONFIG)) {
    try { rootArg = String(JSON.parse(readFileSync(CONFIG, 'utf8')).root || ''); } catch { /* ignore */ }
  }
  return { cwd, rootArg };
}

function agentName(ev) {
  const argv = process.argv.slice(2);
  const ai = argv.indexOf('--agent');
  return (ai >= 0 ? argv[ai + 1] : '') || ev.agent || process.env.AGENTHUB_AGENT || '';
}

/** 只采不注：把拿不到正文的失败事件交给 python 侧落一枚脱敏样本（B067）。 */
function sampleOnly(shape, raw, ev, event) {
  try {
    if (!existsSync(SCRIPT)) { logLine(`skip(sample): ${SCRIPT} 不存在`); return; }
    if (process.env.AGENTHUB_FAIL_SAMPLE === '0') return;
    const { cwd, rootArg } = resolveRoot(ev);
    const tool = String(ev.tool_name || ev.toolName || '');
    const session = String(ev.session_id || ev.sessionId || ev.conversation_id || '');
    const args = [SCRIPT, '--sample-only', '--shape', shape, '--agent', agentName(ev),
      '--cwd', cwd, '--tool', tool, '--session', session, '--event', String(event || ''),
      '--error', String(raw || '').slice(0, 4000)];
    if (rootArg) args.push('--root', rootArg);
    const r = spawnSync(pickPython(), args, {
      encoding: 'utf8', timeout: 5000,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    });
    if (r.status !== 0) {
      logLine(`sample-fail rc=${r.status} err=${String(r.stderr || '').slice(0, 120)}`);
    }
  } catch (e) {
    logLine(`sample-throw ${String(e && e.message || e).slice(0, 120)}`);
  }
}

try {
  const raw = await readStdin();
  let ev = {};
  let broken = '';
  try {
    ev = raw.trim() ? JSON.parse(raw) : {};
    if (ev === null || typeof ev !== 'object') { broken = 'null_event'; ev = {}; }
  } catch { broken = 'bad_json'; ev = {}; }
  const event = String(ev.hook_event_name || ev.hookEventName || ev.event || '');
  const en = event.toLowerCase().replace(/[_-]/g, '');
  const isFailure = en === 'posttoolusefailure';
  const isPost = en === 'posttooluse';
  if (!isFailure && !isPost) {
    // 事件名都拿不到时先留一枚样本再退出。日志里那条 `Cannot read properties of null
    // (reading 'hook_event_name')` 就是 stdin 给了 `null`——宿主确实调了钩子，我们却
    // 什么都不知道。这类形状是 B067 三种成因里的第 ② 种，不采就等于永远只能猜。
    if (broken) sampleOnly(broken, raw, ev, event);
    process.exit(0);
  }

  const tool = String(ev.tool_name || ev.toolName || '');
  const err = errorText(ev);
  if (!err) {
    if (isFailure) {
      // B067：宿主确实报了「这次工具失败」，可我们抽不出正文——这正是那 16 次无签名触发
      // 的入口。以前只写一行本地日志就退出，大脑里什么都不留，于是谁也说不清它是
      // 「事件形状拿不到正文」还是「本来就只有一句人话」。现在落一枚脱敏样本，只采不注。
      logLine(`skip: 拿不到报错正文 tool=${tool.slice(0, 24)}`);
      // 分成两格记：`no_error_text` 是「事件里根本没有正文」，`event_json_dump` 是
      // 「有正文、但那段正文其实就是事件自己」（B045 的 asText 兜底路径）。B045 至今
      // 标着「待确认」，缺的就是这个计数——采到哪一种，修法完全不同。
      sampleOnly(rejectedJson ? 'event_json_dump' : 'no_error_text', raw, ev, event);
    } else if (broken) {
      sampleOnly(broken, raw, ev, event);
    }
    process.exit(0);
  }

  const agent = agentName(ev);
  const session = String(ev.session_id || ev.sessionId || ev.conversation_id || '');
  const { cwd, rootArg } = resolveRoot(ev);
  if (!existsSync(SCRIPT)) { logLine(`skip: ${SCRIPT} 不存在`); process.exit(0); }

  // 命令/入参一起给：报错正文常常只说「exit code 1」，失败的那条命令才是检索线索。
  const input = ev.tool_input || ev.toolInput || {};
  const cmdHint = [input.command, input.cmd, input.prompt, input.file_path, input.path,
    input.url].map(x => (typeof x === 'string' ? x : '')).filter(Boolean).join(' ').slice(0, 400);
  // 必须压成一行再拼：大脑按「失败调用：」这一整行把命令摘掉（brain.py `_error_voice`），
  // 命令自带换行（heredoc、多行脚本）时只有首行被摘，剩下的脚本正文会抢走签名词。
  // 2026-11-02 线上实测：一条 UnicodeEncodeError 的 6 个签名词里 5 个来自我自己正在跑的脚本
  // （`## 四`、`## 五`、`fail hits`），真正的 `unicodeencodeerror` 反被挤到后面。
  const oneLine = cmdHint.replace(/\s+/g, ' ').trim();
  const payload = (oneLine ? `失败调用：${oneLine}\n` : '') + err.slice(0, 6000);

  const args = [SCRIPT, '--agent', agent, '--cwd', cwd, '--tool', tool,
    '--session', session, '--max-chars', String(MAX_CHARS), '--event', event,
    '--error', payload];
  if (rootArg) args.push('--root', rootArg);
  if (process.env.AGENTHUB_ERROR_STATE) args.push('--state', process.env.AGENTHUB_ERROR_STATE);
  if (process.env.AGENTHUB_ERROR_NO_JOURNAL === '1') args.push('--no-journal');

  const r = spawnSync(pickPython(), args, {
    encoding: 'utf8', timeout: 5000,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  });
  const text = String(r.stdout || '').trim();
  if (!text) {
    if (r.status !== 0) logLine(`empty rc=${r.status} err=${String(r.stderr || '').slice(0, 120)}`);
    process.exit(0);
  }
  const capped = text.length > MAX_CHARS ? text.slice(0, MAX_CHARS - 16) + '\n…（已截断）' : text;
  logLine(`error_recall ${capped.length} chars agent=${agent} tool=${tool.slice(0, 24)} sid=${session.slice(0, 24)}`);
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: isFailure ? 'PostToolUseFailure' : 'PostToolUse',
      additionalContext: capped,
    },
  }));
} catch (e) {
  logLine(`error ${String(e && e.message || e).slice(0, 160)}`);
  process.exit(0);
}
