#!/usr/bin/env node
/**
 * AgentHub 记忆钩子 · hermes 版（R815 简报 / R821 按轮回忆）。
 *
 * hermes 没有独立的 SessionStart 出口，只有 pre_llm_call——每次模型调用都会触发。
 * 分工：
 *   - 每个 session 的第一次调用注入 1.2k 简报（每次都注入会持续烧 token）；
 *   - 之后每次带着新的 user_message 时，按这句话检索大脑并注入相关记忆；
 *   - 同一句话 30 分钟内不重复注入（pre_llm_call 可能一轮触发多次）。
 * 也兼容 userpromptsubmit 事件（若 hermes 版本提供），此时只做回忆、输出
 * Claude 系 hookSpecificOutput 契约。
 *
 * 契约：pre_llm_call 输出 {"context": "…"}（与 agent_guard.mjs 已在本机验证）；
 * UserPromptSubmit 输出 {"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",
 * "additionalContext":"…"}}。任何异常静默 exit 0，绝不影响模型调用。
 */
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';

const HOME = homedir();
const LOG = process.env.AGENTHUB_HERMES_LOG
  || path.join(HOME, '.workbuddy', 'logs', 'brain-inject.log');
const CONFIG = path.join(HOME, '.agenthub', 'config.json');
const STATE = process.env.AGENTHUB_HERMES_STATE
  || path.join(HOME, '.agenthub', 'state', 'brief-hermes.json');
const BRIEF = process.env.AGENTHUB_BRIEF_SCRIPT
  || path.join(HOME, '.agenthub', 'mcp_server', 'brain_brief.py');
const RECALL = process.env.AGENTHUB_RECALL_SCRIPT
  || path.join(HOME, '.agenthub', 'mcp_server', 'brain_recall.py');
const MAX_CHARS = Math.max(400, Number(process.env.AGENTHUB_BRIEF_MAX_CHARS) || 3000);
const RECALL_MAX_CHARS = Math.max(300, Number(process.env.AGENTHUB_RECALL_MAX_CHARS) || 900);
const TTL_MS = 7 * 24 * 3600 * 1000;
const GLOBAL_COOLDOWN_MS = 3600 * 1000;
const PROMPT_DEDUPE_MS = 30 * 60 * 1000;

function logLine(msg) {
  try {
    mkdirSync(path.dirname(LOG), { recursive: true });
    appendFileSync(LOG, `${new Date().toISOString()}  hermes  ${msg}\n`, 'utf8');
  } catch { /* ignore */ }
}

function readState() {
  try {
    const s = JSON.parse(readFileSync(STATE, 'utf8'));
    if (s && typeof s === 'object' && s.sids && typeof s.sids === 'object') {
      if (!s.last || typeof s.last !== 'object') s.last = {};
      return s;
    }
  } catch { /* ignore */ }
  return { v: 1, sids: {}, last: {} };
}

function saveState(s, key, promptHash) {
  try {
    const now = Date.now();
    const kept = Object.entries(s.sids)
      .filter(([, ts]) => now - Number(ts) < TTL_MS)
      .sort((a, b) => Number(b[1]) - Number(a[1]))
      .slice(0, 200);
    s.sids = Object.fromEntries(kept);
    s.sids[key] = now;
    s.last = Object.fromEntries(Object.entries(s.last || {})
      .filter(([, v]) => v && now - Number(v.ts) < PROMPT_DEDUPE_MS)
      .slice(-50));
    if (promptHash) s.last[key] = { h: promptHash, ts: now };
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

function extractPrompt(ev) {
  const direct = [ev.prompt, ev.user_prompt, ev.userPrompt, ev.user_message,
    ev.userMessage, ev.prompt_text];
  for (const v of direct) {
    if (typeof v === 'string' && v.trim()) return v.trim();
  }
  if (typeof ev.message === 'string' && ev.message.trim()) return ev.message.trim();
  const msgs = Array.isArray(ev.messages) ? ev.messages : [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (!m || (m.role && m.role !== 'user')) continue;
    if (typeof m.content === 'string' && m.content.trim()) return m.content.trim();
    if (Array.isArray(m.content)) {
      const text = m.content
        .map((p) => (typeof p === 'string' ? p : (p && p.text) || ''))
        .join(' ').trim();
      if (text) return text;
    }
  }
  return '';
}

function rootArg() {
  let root = process.env.AGENTHUB_ROOT || '';
  if (!root && existsSync(CONFIG)) {
    try { root = String(JSON.parse(readFileSync(CONFIG, 'utf8')).root || ''); } catch { /* ignore */ }
  }
  return root;
}

function runScript(script, args, budget) {
  if (!existsSync(script)) {
    logLine(`skip: ${script} 不存在`);
    return '';
  }
  const r = spawnSync(pickPython(), [script, ...args], {
    encoding: 'utf8', timeout: 6000,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  });
  const text = String(r.stdout || '').trim();
  if (!text) {
    logLine(`empty ${path.basename(script)} (rc=${r.status}, err=${String(r.stderr || '').slice(0, 120)})`);
    return '';
  }
  return text.length > budget ? text.slice(0, budget - 16) + '\n…（已截断）' : text;
}

try {
  const raw = await readStdin();
  let ev = {};
  try { ev = raw.trim() ? JSON.parse(raw) : {}; } catch { ev = {}; }
  const event = String(ev.hook_event_name || ev.hookEventName || 'pre_llm_call');
  const en = event.toLowerCase().replace(/[_-]/g, '');
  const isPreLlm = en === 'prellmcall';
  const isPrompt = en === 'userpromptsubmit' || en === 'userpromptsubmitted';
  if (!isPreLlm && !isPrompt) process.exit(0);

  const argv = process.argv.slice(2);
  const ai = argv.indexOf('--agent');
  const agent = (ai >= 0 ? argv[ai + 1] : '') || process.env.AGENTHUB_AGENT || 'hermes-cn';
  const cwd = ev.cwd || ev.workingDirectory || process.cwd();
  const sid = String(ev.session_id || ev.sessionId || '').trim();
  const key = sid || '_global';
  const prompt = extractPrompt(ev);
  const root = rootArg();
  const base = ['--agent', agent, '--cwd', cwd];
  if (root) base.push('--root', root);

  const st = readState();
  const last = Number(st.sids[key] || 0);
  const briefDue = isPreLlm && (!last || Date.now() - last >= (sid ? TTL_MS : GLOBAL_COOLDOWN_MS));
  const parts = [];

  if (briefDue) {
    const text = runScript(BRIEF, [...base, '--max-chars', String(MAX_CHARS)], MAX_CHARS);
    if (text) parts.push(text);
  }

  let promptHash = '';
  if (prompt && prompt.length >= 6 && !prompt.startsWith('/')) {
    promptHash = createHash('sha1').update(prompt).digest('hex').slice(0, 16);
    const prev = st.last[key];
    const dup = prev && prev.h === promptHash && Date.now() - Number(prev.ts) < PROMPT_DEDUPE_MS;
    if (!dup) {
      const text = runScript(RECALL, [...base,
        '--prompt', prompt.slice(0, 8000), '--session', sid,
        '--max-chars', String(RECALL_MAX_CHARS)], RECALL_MAX_CHARS);
      if (text) parts.push(text);
    }
  }

  if (!parts.length) process.exit(0);
  const context = parts.join('\n\n');
  saveState(st, key, promptHash);
  if (isPrompt) {
    process.stdout.write(JSON.stringify({
      hookSpecificOutput: { hookEventName: 'UserPromptSubmit', additionalContext: context },
    }));
  } else {
    process.stdout.write(JSON.stringify({ context }));
  }
  logLine(`${briefDue ? 'brief' : 'recall'} ${context.length} chars sid=${key} agent=${agent}`);
} catch (e) {
  try { logLine(`error: ${e && e.message}`); } catch { /* ignore */ }
  process.exit(0);
}
