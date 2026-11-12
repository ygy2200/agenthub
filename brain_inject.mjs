#!/usr/bin/env node
/**
 * AgentHub SessionStart 钩子：把大脑简报注入会话（R811）。
 *
 * 病根：引导块写「开工先 hub_get_progress」只是建议，模型可以不理——真实库
 * doctor 实测 claude/dsh/hermes 配置了 MCP 却近 7 天零调用。本钩子不再建议，
 * 而是把「团队最近进展 / 当前项目相关记忆 / 置顶记忆 / 未读信号 / 我的任务」
 * 直接送进会话（additionalContext），AI 一开局就有上下文可用。
 *
 * 契约（Claude/WorkBuddy 同源，Qoder 亦识别）：
 *   stdout {"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"…"}}
 * 安全：只读本地库；Python 侧失败/超时/无输出 → 静默 exit 0，绝不影响会话启动。
 * 预算：注入 ≤3000 字符（brain 侧还有硬预算），避免上下文被简报挤占。
 */
import { spawnSync } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HOME = homedir();
const HERE = path.dirname(fileURLToPath(import.meta.url));
const LOG = path.join(HOME, '.workbuddy', 'logs', 'brain-inject.log');
const CONFIG = path.join(HOME, '.agenthub', 'config.json');
const HOME_SCRIPT = path.join(HOME, '.agenthub', 'mcp_server', 'brain_brief.py');
// R1122：未部署环境（CI / 源码树）没有 HOME 副本，从钩子自身目录取仓库副本。
const REPO_SCRIPT = path.join(HERE, 'brain_brief.py');
const SCRIPT = process.env.AGENTHUB_BRIEF_SCRIPT
  || (existsSync(HOME_SCRIPT) ? HOME_SCRIPT
      : (existsSync(REPO_SCRIPT) ? REPO_SCRIPT : HOME_SCRIPT));
const MAX_CHARS = Math.max(400, Number(process.env.AGENTHUB_BRIEF_MAX_CHARS) || 3000);

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
  const fixed = ['D:/python311/python.exe', 'D:/Python311/python.exe'];
  for (const p of fixed) { if (existsSync(p)) return p; }
  return 'python';
}

try {
  const raw = await readStdin();
  let ev = {};
  try { ev = raw.trim() ? JSON.parse(raw) : {}; } catch { ev = {}; }
  const event = String(ev.hook_event_name || ev.hookEventName || 'SessionStart');
  const en = event.toLowerCase().replace(/[_-]/g, '');
  if (en !== 'sessionstart' && en !== 'sessionbegin') process.exit(0);

  const argv = process.argv.slice(2);
  const ai = argv.indexOf('--agent');
  const agent = (ai >= 0 ? argv[ai + 1] : '') || ev.agent || process.env.AGENTHUB_AGENT || '';
  const cwd = ev.cwd || ev.workingDirectory || ev.cwdPath || process.cwd();
  if (!existsSync(SCRIPT)) { logLine(`skip: ${SCRIPT} 不存在`); process.exit(0); }

  const py = pickPython();
  // R923：会话号一起传给简报入口——没有它，必做规则的注入就无法回头判定有没有被照做
  const session = String(ev.session_id || ev.sessionId || ev.conversation_id || '');
  const args = [SCRIPT, '--agent', agent, '--cwd', cwd, '--max-chars', String(MAX_CHARS)];
  if (session) args.push('--session', session);
  let rootArg = process.env.AGENTHUB_ROOT || '';
  if (!rootArg && existsSync(CONFIG)) {
    try { rootArg = String(JSON.parse(readFileSync(CONFIG, 'utf8')).root || ''); } catch { /* ignore */ }
  }
  if (rootArg) args.push('--root', rootArg);
  const r = spawnSync(py, args, {
    encoding: 'utf8', timeout: 6000,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  });
  const text = String(r.stdout || '').trim();
  if (!text) { logLine(`empty (rc=${r.status}, err=${String(r.stderr || '').slice(0, 120)})`); process.exit(0); }
  const capped = text.length > MAX_CHARS ? text.slice(0, MAX_CHARS - 16) + '\n…（已截断）' : text;
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: { hookEventName: 'SessionStart', additionalContext: capped },
  }));
  logLine(`inject ${capped.length} chars agent=${agent || '?'} session=${session || '-'} cwd=${cwd}`);
} catch (e) {
  try { logLine(`error: ${e && e.message}`); } catch { /* ignore */ }
  process.exit(0);
}
