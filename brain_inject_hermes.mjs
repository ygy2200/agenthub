#!/usr/bin/env node
/**
 * AgentHub 简报钩子 · hermes 版（R815）：pre_llm_call 首次调用注入。
 *
 * hermes 没有 SessionStart 出口，只有 pre_llm_call——每次模型调用都会触发。
 * 每次都注入 1.2k 简报会持续烧 token，所以按 session_id 只注入一次；没拿到 sid
 * 时退化成全局冷却（1h），避免每轮重复注入。提交成功后才记 sid（失败下次重试）。
 *
 * 契约：stdout {"context": "…"}（与 agent_guard.mjs 已在 hermes 上验证的提醒契约同源）。
 * 安全：只读本地库；任何异常静默 exit 0，绝不影响模型调用。
 */
import { spawnSync } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';

const HOME = homedir();
const LOG = path.join(HOME, '.workbuddy', 'logs', 'brain-inject.log');
const CONFIG = path.join(HOME, '.agenthub', 'config.json');
const STATE = path.join(HOME, '.agenthub', 'state', 'brief-hermes.json');
const SCRIPT = process.env.AGENTHUB_BRIEF_SCRIPT
  || path.join(HOME, '.agenthub', 'mcp_server', 'brain_brief.py');
const MAX_CHARS = Math.max(400, Number(process.env.AGENTHUB_BRIEF_MAX_CHARS) || 3000);
const TTL_MS = 7 * 24 * 3600 * 1000;
const GLOBAL_COOLDOWN_MS = 3600 * 1000;

function logLine(msg) {
  try {
    mkdirSync(path.dirname(LOG), { recursive: true });
    appendFileSync(LOG, `${new Date().toISOString()}  hermes  ${msg}\n`, 'utf8');
  } catch { /* ignore */ }
}

function readState() {
  try {
    const s = JSON.parse(readFileSync(STATE, 'utf8'));
    if (s && typeof s === 'object' && s.sids && typeof s.sids === 'object') return s;
  } catch { /* ignore */ }
  return { v: 1, sids: {} };
}

function saveState(s, key) {
  try {
    const now = Date.now();
    const kept = Object.entries(s.sids)
      .filter(([, ts]) => now - Number(ts) < TTL_MS)
      .sort((a, b) => Number(b[1]) - Number(a[1]))
      .slice(0, 200);
    s.sids = Object.fromEntries(kept);
    s.sids[key] = now;
    mkdirSync(path.dirname(STATE), { recursive: true });
    writeFileSync(STATE, JSON.stringify(s), 'utf8');
  } catch { /* ignore */ }
}

async function readStdin() {
  const chunks = [];
  try { for await (const c of process.stdin) chunks.push(c); } catch { /* ignore */ }
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

try {
  const raw = await readStdin();
  let ev = {};
  try { ev = raw.trim() ? JSON.parse(raw) : {}; } catch { ev = {}; }
  const event = String(ev.hook_event_name || ev.hookEventName || 'pre_llm_call');
  const en = event.toLowerCase().replace(/[_-]/g, '');
  if (en !== 'prellmcall') process.exit(0);

  const sid = String(ev.session_id || ev.sessionId || '').trim();
  const key = sid || '_global';
  const st = readState();
  const last = Number(st.sids[key] || 0);
  const ttl = sid ? TTL_MS : GLOBAL_COOLDOWN_MS;
  if (last && Date.now() - last < ttl) process.exit(0);

  const argv = process.argv.slice(2);
  const ai = argv.indexOf('--agent');
  const agent = (ai >= 0 ? argv[ai + 1] : '') || process.env.AGENTHUB_AGENT || 'hermes-cn';
  const cwd = ev.cwd || ev.workingDirectory || process.cwd();
  if (!existsSync(SCRIPT)) { logLine(`skip: ${SCRIPT} 不存在`); process.exit(0); }

  const args = [SCRIPT, '--agent', agent, '--cwd', cwd, '--max-chars', String(MAX_CHARS)];
  let rootArg = process.env.AGENTHUB_ROOT || '';
  if (!rootArg && existsSync(CONFIG)) {
    try { rootArg = String(JSON.parse(readFileSync(CONFIG, 'utf8')).root || ''); } catch { /* ignore */ }
  }
  if (rootArg) args.push('--root', rootArg);
  const r = spawnSync(pickPython(), args, {
    encoding: 'utf8', timeout: 6000,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  });
  const text = String(r.stdout || '').trim();
  if (!text) { logLine(`empty (rc=${r.status}, err=${String(r.stderr || '').slice(0, 120)})`); process.exit(0); }
  const capped = text.length > MAX_CHARS ? text.slice(0, MAX_CHARS - 16) + '\n…（已截断）' : text;
  saveState(st, key);
  process.stdout.write(JSON.stringify({ context: capped }));
  logLine(`inject ${capped.length} chars sid=${key} agent=${agent}`);
} catch (e) {
  try { logLine(`error: ${e && e.message}`); } catch { /* ignore */ }
  process.exit(0);
}
