#!/usr/bin/env node
/**
 * AgentHub 即时回忆钩子（R820）：用户每说一句话，按这句话检索大脑并按需注入。
 *
 * 病根：SessionStart 简报只解决「开局」，用户后来说「上次那个发布脚本怎么修的」
 * 时仍要自己提醒「先查大脑」。本钩子挂在 UserPromptSubmit（Claude/Qoder/Codex/
 * WorkBuddy/DSH 同源契约），把相关记忆直接放进这一轮的 additionalContext。
 *
 * 与 SessionStart 的分工：简报是固定快照，本钩子是查询结果；没有命中就静默
 * （不输出），避免每条消息都塞「无相关记忆」的噪声。
 * 安全：只读本地库；Python 失败/超时/无输出 → exit 0；不阻塞用户发言。
 */
import { spawnSync } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';

const HOME = homedir();
const LOG = path.join(HOME, '.workbuddy', 'logs', 'brain-recall.log');
const CONFIG = path.join(HOME, '.agenthub', 'config.json');
const SCRIPT = process.env.AGENTHUB_RECALL_SCRIPT
  || path.join(HOME, '.agenthub', 'mcp_server', 'brain_recall.py');
const MAX_CHARS = Math.max(300, Number(process.env.AGENTHUB_RECALL_MAX_CHARS) || 900);

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

try {
  const raw = await readStdin();
  let ev = {};
  try { ev = raw.trim() ? JSON.parse(raw) : {}; } catch { ev = {}; }
  const event = String(ev.hook_event_name || ev.hookEventName || ev.event || 'UserPromptSubmit');
  const en = event.toLowerCase().replace(/[_-]/g, '');
  if (en !== 'userpromptsubmit' && en !== 'userpromptsubmitted') process.exit(0);

  const prompt = String(ev.prompt || ev.user_prompt || ev.userPrompt
    || ev.prompt_text || ev.message || '').trim();
  if (!prompt || prompt.startsWith('/')) process.exit(0);

  const argv = process.argv.slice(2);
  const ai = argv.indexOf('--agent');
  const agent = (ai >= 0 ? argv[ai + 1] : '') || ev.agent || process.env.AGENTHUB_AGENT || '';
  const session = String(ev.session_id || ev.sessionId || ev.conversation_id || '');
  const cwd = ev.cwd || ev.workingDirectory || ev.cwdPath || process.cwd();
  // R863：宿主给了 transcript 时，短句/推进句（继续/接着弄/重启了继续）也能交给
  // Python 用「上一轮话题」补检索词；没给 transcript 就维持 R820 的短句静默。
  // 注意：Qoder Desktop 的载荷实测不带 transcript_path，但 Python 侧能按 session_id
  // 找到本地会话文件——所以 CONT_RE 命中的短句一律放行，由 Python 决定找不找得到。
  const tp = String(ev.transcript_path || ev.transcriptPath || '');
  const hasTranscript = !!tp && existsSync(tp);
  const CONT_RE = /(继续|接着|然后|下一步|重启|往下|开始吧|接着干)/;
  if (prompt.length < 6 && !CONT_RE.test(prompt)) process.exit(0);
  if (!existsSync(SCRIPT)) { logLine(`skip: ${SCRIPT} 不存在`); process.exit(0); }

  const args = [SCRIPT, '--agent', agent, '--cwd', cwd,
    '--prompt', prompt.slice(0, 8000), '--session', session,
    '--max-chars', String(MAX_CHARS)];
  if (hasTranscript) args.push('--transcript', tp);
  let rootArg = process.env.AGENTHUB_ROOT || '';
  if (!rootArg && existsSync(CONFIG)) {
    try { rootArg = String(JSON.parse(readFileSync(CONFIG, 'utf8')).root || ''); } catch { /* ignore */ }
  }
  if (rootArg) args.push('--root', rootArg);
  if (process.env.AGENTHUB_RECALL_STATE) args.push('--state', process.env.AGENTHUB_RECALL_STATE);
  if (process.env.AGENTHUB_RECALL_NO_JOURNAL === '1') args.push('--no-journal');
  // 装置/探针跑部署副本时不许把曝光账记脏（R861 的教训）：默认 live，显式才降级。
  const origin = process.env.AGENTHUB_RECALL_ORIGIN || '';
  if (origin === 'probe' || origin === 'eval') args.push('--origin', origin);

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
  logLine(`recall ${capped.length} chars agent=${agent} sid=${session.slice(0, 24)}`);
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: { hookEventName: 'UserPromptSubmit', additionalContext: capped },
  }));
} catch (e) {
  logLine(`error ${String(e && e.message || e).slice(0, 160)}`);
  process.exit(0);
}
