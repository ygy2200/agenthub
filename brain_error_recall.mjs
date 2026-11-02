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
function errorText(ev) {
  for (const k of ['error', 'errorMessage', 'error_message', 'failure',
    'tool_error', 'stderr', 'message']) {
    const t = asText(ev?.[k]);
    if (t.trim().length >= MIN_ERR_CHARS) return t.trim();
  }
  const resp = ev?.tool_response ?? ev?.toolResult ?? ev?.result;
  const isErr = resp?.isError ?? resp?.is_error ?? resp?.error ?? resp?.ok === false;
  const t = asText(resp);
  if (isErr != null && isErr !== false && t.trim().length >= MIN_ERR_CHARS) return t.trim();
  return '';
}

try {
  const raw = await readStdin();
  let ev = {};
  try { ev = raw.trim() ? JSON.parse(raw) : {}; } catch { ev = {}; }
  const event = String(ev.hook_event_name || ev.hookEventName || ev.event || '');
  const en = event.toLowerCase().replace(/[_-]/g, '');
  const isFailure = en === 'posttoolusefailure';
  const isPost = en === 'posttooluse';
  if (!isFailure && !isPost) process.exit(0);

  const tool = String(ev.tool_name || ev.toolName || '');
  const err = errorText(ev);
  if (!err) {
    if (isFailure) logLine(`skip: 拿不到报错正文 tool=${tool.slice(0, 24)}`);
    process.exit(0);
  }

  const argv = process.argv.slice(2);
  const ai = argv.indexOf('--agent');
  const agent = (ai >= 0 ? argv[ai + 1] : '') || ev.agent || process.env.AGENTHUB_AGENT || '';
  const session = String(ev.session_id || ev.sessionId || ev.conversation_id || '');
  const cwd = ev.cwd || ev.workingDirectory || ev.cwdPath || process.cwd();
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
    '--session', session, '--max-chars', String(MAX_CHARS), '--error', payload];
  let rootArg = process.env.AGENTHUB_ROOT || '';
  if (!rootArg && existsSync(CONFIG)) {
    try { rootArg = String(JSON.parse(readFileSync(CONFIG, 'utf8')).root || ''); } catch { /* ignore */ }
  }
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
