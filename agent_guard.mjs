#!/usr/bin/env node
/**
 * AgentHub 规则守卫（跨 agent 通用版）——把「干完活必须落大脑」变成硬闸门。
 *
 * 背景：WorkBuddy 侧已有 guard-brainlog.mjs（present_files 闸门）。本脚本是把同一
 * 规则推广到其他 agent（Claude Code / Codex / DSH / hermes），它们没有 present_files，
 * 故闸门改挂在 **Stop 事件**上（"想收工？先记大脑"）。
 *
 * 事件契约（各家 hook 事件名与 WorkBuddy/Claude 同源）：
 *   SessionStart        → 初始化本会话状态（干净起步）
 *   PreToolUse(hub_*)   → 最小校验通过（content 非空、非 dry_run）才置 logged=true
 *   PreToolUse(其他工具) → 记 worked=true（说明本会话"干了活"，Stop 时才需要闸门）
 *   Stop / SessionEnd   → 闸门：worked 且未记大脑 → 阻断（最多 2 次，防死循环）
 *
 * 工具名归一（R8，2026-10-20）：守卫原生剥壳，不再需要每家维护适配器。
 *   直接调用：mcp__agenthub__hub_log_work / hub_log_work
 *   多路复用：mcp_call / hub_call_tool / mcp__agenthub__hub_call_tool
 *             （内层 toolName + arguments，允许套娃；最多剥 3 层）
 *   Qoder 曾是唯一例外（客户端 agent 枚举不含 qoder，写大脑只能走 hub_call_tool），
 *   其专属 agenthub_guard_adapter.mjs 因此存在；现已不需要。
 *
 * 输出契约：
 *   PreToolUse 拒绝 → {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}
 *   Stop 阻断      → {"decision":"block","reason":"..."}   ← Claude 系 Stop 钩子标准；不认识的家会忽略（fail-safe）
 *   SessionStart / 放行 → 无输出（静默）
 *
 * 状态：~/.workbuddy/state/agent-guard.json = { v:1, sessions:{ "<sid>":{startTs,logged,worked,blocks,...} } }
 *   按 session_id 隔离（不串会话）；48h / 50 条自动清理。
 * 逃生门：ASU_SKIP_BRAINLOG=1（跳过即写审计日志）。
 * 任何异常一律 exit 0（fail-open，绝不阻塞正常会话）。
 */
import { readFileSync, writeFileSync, mkdirSync, appendFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';

const STATE = path.join(homedir(), '.workbuddy', 'state', 'agent-guard.json');
const LOG = path.join(homedir(), '.workbuddy', 'logs', 'agent-guard.log');
const NOSID = '_nosid';
const MAX_SESSIONS = 50;
const SESSION_TTL_MS = 48 * 3600 * 1000;
const MAX_BLOCKS = 2;              // 单会话最多阻断次数，防死循环

// 身份：由各家 hook 配置以 `--agent <name>` 传入（也支持 env AGENTHUB_AGENT）。
// 目的：让守卫的判定能按 agent 归因，供大脑 ops 检查「规则执行」健康度。
const _argv = process.argv.slice(2);
const _ai = _argv.indexOf('--agent');
const AGENT = (_ai >= 0 && _argv[_ai + 1]) || process.env.AGENTHUB_AGENT || 'unknown';

function logLine(m) {
  try { mkdirSync(path.dirname(LOG), { recursive: true }); appendFileSync(LOG, `${new Date().toISOString()}  agent=${AGENT}  ${m}\n`, 'utf8'); } catch { /* ignore */ }
}
function readState() {
  try {
    const s = JSON.parse(readFileSync(STATE, 'utf8'));
    if (s && typeof s === 'object' && s.sessions && typeof s.sessions === 'object') return s;
  } catch { /* ignore */ }
  return { v: 1, sessions: {} };
}
function prune(s) {
  const now = Date.now();
  const kept = Object.entries(s.sessions || {})
    .filter(([, v]) => v && typeof v.startTs === 'number' && now - v.startTs < SESSION_TTL_MS)
    .sort((a, b) => (b[1].startTs || 0) - (a[1].startTs || 0))
    .slice(0, MAX_SESSIONS);
  s.sessions = Object.fromEntries(kept);
  return s;
}
function writeState(s) {
  try { mkdirSync(path.dirname(STATE), { recursive: true }); writeFileSync(STATE, JSON.stringify(prune(s)), 'utf8'); } catch { /* ignore */ }
}
async function readStdin() {
  const c = [];
  try { for await (const x of process.stdin) c.push(x); } catch { /* ignore */ }
  return Buffer.concat(c).toString('utf8');
}

const raw = await readStdin();
let ev = null;
try { ev = raw && raw.trim() ? JSON.parse(raw) : null; } catch { ev = null; }
if (!ev) process.exit(0);

const event = ev.hook_event_name || '';
// 事件名归一：兼容 Claude/Codex/WorkBuddy（SessionStart/PreToolUse/Stop）
// 与 hermes（session_start/pre_tool_call/pre_llm_call/subagent_stop）
const en = event.toLowerCase().replace(/[_-]/g, '');
const isReset = en === 'sessionstart' || en === 'sessionbegin';
const isPre = en === 'pretooluse' || en === 'pretoolcall';
const isStop = en === 'stop' || en === 'sessionend' || en === 'subagentstop' || en === 'turnstop';
const isPreLlm = en === 'prellmcall';

// 工具名归一：剥掉 MCP 前缀，并解开 mcp_call / hub_call_tool 的嵌套参数。
function leafTool(name) {
  const s = String(name || '');
  const m = s.match(/^mcp__.+?__(.+)$/);
  return m ? m[1] : s;
}
function isMultiplexer(name) {
  return /^(?:mcp__.+?__)?(?:hub_call_tool|mcp_call)$/i.test(String(name || ''));
}
function resolveTool(ev) {
  let name = String(ev.tool_name || ev.toolName || '');
  let input = (ev.tool_input && typeof ev.tool_input === 'object') ? ev.tool_input
            : (ev.toolInput && typeof ev.toolInput === 'object') ? ev.toolInput : {};
  const raw = name;
  for (let depth = 0; depth < 3 && isMultiplexer(name); depth++) {
    const inner = input.toolName || input.tool_name || input.name || input.tool;
    if (typeof inner !== 'string' || !inner.trim()) break;
    const args = input.arguments || input.params || input.input;
    name = inner.trim();
    input = (args && typeof args === 'object') ? args : {};
  }
  return { raw, name, leaf: leafTool(name), input };
}
const _call = resolveTool(ev);
const tool = _call.leaf;
const toolInput = _call.input;
// 日志里保留原始形态，嵌套调用一眼可见（tool=mcp_call→hub_memory_write）
const toolLabel = (_call.raw && _call.raw !== tool) ? `${_call.raw}→${tool}` : tool;
const sid = (ev.session_id || ev.sessionId || '').toString();
const key = sid || NOSID;

// 1) SessionStart → 重置本会话
if (isReset) {
  const s = readState();
  s.sessions[key] = { startTs: Date.now(), logged: false, worked: false, blocks: 0, reminded: false };
  writeState(s);
  logLine(`reset@${event} sid=${key}`);
  process.exit(0);
}

// 逃生门 + 审计
if (process.env.ASU_SKIP_BRAINLOG === '1') {
  logLine(`SKIP(ASU_SKIP_BRAINLOG) event=${event} tool=${toolLabel} sid=${key}`);
  process.exit(0);
}

const isHub = /\bhub_log_work\b|\bhub_memory_write\b/.test(tool);

// 2) PreToolUse / pre_tool_call
if (isPre) {
  const s = readState();
  const cur = s.sessions[key] || { startTs: Date.now(), logged: false, worked: false, blocks: 0, reminded: false };

  if (isHub) {
    // 最小校验：content 非空 且 非 dry_run 才计数（防空调用翻牌）
    const ti = toolInput;
    const dry = ti.dry_run === true;
    const content = typeof ti.content === 'string' ? ti.content.trim() : '';
    if (!dry && content.length > 0) {
      cur.logged = true;
      cur.lastHubTs = Date.now();
      cur.lastHubTool = tool;
      logLine(`marked logged via ${toolLabel} sid=${key}`);
    } else {
      logLine(`NOT counted (${dry ? 'dry_run' : 'empty content'}) via ${toolLabel} sid=${key}`);
    }
    s.sessions[key] = cur;
    writeState(s);
    process.exit(0);   // hub 调用本身永远放行
  }

  // 其他工具 = 干了活；顺带保留 WorkBuddy 的 present_files 闸门（同款脚本可复用）
  cur.worked = true;
  cur.workTools = (cur.workTools || 0) + 1;
  s.sessions[key] = cur;

  if (/present_files/.test(tool)) {
    if (cur.logged === true) {
      writeState(s);
      logLine(`present_files allowed (logged) sid=${key}`);
      process.exit(0);
    }
    writeState(s);
    logLine(`DENY present_files (not logged) sid=${key}`);
    process.stdout.write(JSON.stringify({
      hookSpecificOutput: {
        hookEventName: 'PreToolUse',
        permissionDecision: 'deny',
        permissionDecisionReason:
          '交付闸门：本次会话尚未向 AgentHub 大脑记工作（hub_log_work / hub_memory_write）。' +
          '请先调用 hub_log_work（project/agent/content）+ hub_memory_write（kind/tags/content），再重试。' +
          '若确无可记（纯闲聊 / 仅读文件），设 ASU_SKIP_BRAINLOG=1 后重试。'
      }
    }));
    process.exit(0);
  }

  writeState(s);
  process.exit(0);
}

// 2b) pre_llm_call（hermes）：干过活但未记大脑 → 注入提醒（每会话一次）
if (isPreLlm) {
  const s = readState();
  const cur = s.sessions[key];
  if (!cur) { process.exit(0); }
  if (cur.logged === true || cur.worked !== true || cur.reminded === true) { process.exit(0); }
  cur.reminded = true;
  s.sessions[key] = cur;
  writeState(s);
  logLine(`REMIND@${event} (worked 未记大脑) sid=${key}`);
  process.stdout.write(JSON.stringify({
    context: '【AgentHub 铁律提醒】本会话干了活但还没向大脑记工作。收工前请完成：' +
      '① hub_log_work(project, agent, content)（含 目的/做了什么/验证结果/如何回滚）' +
      '② hub_memory_write(content, kind, tags)。（hermes 平台只能在 pre_tool_call 阻断，' +
      '无法在收工处硬拦，故此处软提醒）'
  }));
  process.exit(0);
}

// 3) Stop / SessionEnd / subagent_stop → 闸门：干过活但没记大脑就阻断（最多 MAX_BLOCKS 次）
if (isStop) {
  const s = readState();
  const cur = s.sessions[key];
  if (!cur) { logLine(`gate@${event} 无会话状态，放行 sid=${key}`); process.exit(0); }
  if (cur.logged === true) { logLine(`gate@${event} allowed (logged) sid=${key}`); process.exit(0); }
  if (cur.worked !== true) { logLine(`gate@${event} allowed (无工具活动) sid=${key}`); process.exit(0); }
  if ((cur.blocks || 0) >= MAX_BLOCKS) {
    cur.blocks = (cur.blocks || 0);
    s.sessions[key] = cur;
    writeState(s);
    logLine(`gate@${event} 已阻断${cur.blocks}次仍未见记录，放行避免死循环 sid=${key}`);
    process.exit(0);
  }
  cur.blocks = (cur.blocks || 0) + 1;
  s.sessions[key] = cur;
  writeState(s);
  logLine(`BLOCK@${event} (worked 但未记大脑, 第${cur.blocks}次) sid=${key}`);
  process.stdout.write(JSON.stringify({
    decision: 'block',
    reason:
      'AgentHub 铁律：本会话干了活但没有向大脑记工作。收工前请先完成：' +
      '① hub_log_work(project, agent, content)（含 目的/做了什么/验证结果/如何回滚）' +
      '② hub_memory_write(content, kind, tags)（有跨会话价值的知识）。' +
      `（本会话最多拦截 ${MAX_BLOCKS} 次；若确无可记或工具不可用，设环境变量 ASU_SKIP_BRAINLOG=1 后重试）`
  }));
  process.exit(0);
}

process.exit(0);
