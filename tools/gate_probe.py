#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闸门生效性实测探针（R63）——对「配置在位但零触发」的 agent 做契约级验证。

背景：rules_health（R32）能把静默闸门分成 dead（近期有记录却零触发=钩子疑似失效）
与 idle（无活动=未获验证），但 idle 只是「未验证」。本脚本把「未验证」往前推一步：
按各 agent 的 hook 事件词汇表构造与生产同形的载荷，实跑一次 agent_guard.mjs，
再回读守卫日志确认归因行出现——证明「钩子接了线、灯会亮」。

每个 agent 跑两轮：
  轮一（放行路径）session_start → pre_tool_call(hub_log_work, 非空 content) → stop
    期望日志：reset@… / marked logged via hub_log_work / gate@… allowed (logged)
  轮二（阻断路径）session_start → pre_tool_call(普通工具=干了活) → stop
    期望：stdout 出 {"decision":"block",…} 且日志有 BLOCK 行——闸门真能拦。

证明的与不能证明的（边界如实标注）：
  ✓ 守卫脚本在该 agent 的生产调用形态（node 路径 + --agent + 事件词汇 + 载荷字段）
    下能执行、能归因、能阻断；
  ✗ 不证明该 agent 的 hook Runner 真的会以这个形态调用——那要等它跑一次真实会话。
    所以这是契约级实测，不是端到端实测。

zcode 不在范围内：它的闸门是 auto_log.py（另一套机制），证据通道是
source='auto' 的大脑记录（见 ops.rules_components 的 auto_records 通道）。

副作用（诚实声明）：只写 ~/.workbuddy/logs/agent-guard.log（探针行）与
~/.workbuddy/state/agent-guard.json（probe-* 会话，48h 自动清理）。不碰大脑库。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

GUARD = Path.home() / ".agenthub" / "hooks" / "agent_guard.mjs"
LOG = Path.home() / ".workbuddy" / "logs" / "agent-guard.log"
NODE = "D:/node/node.exe"

# (agent, reset 事件名, pre 事件名, stop 事件名)——词汇表取自守卫的事件归一处，
# 与各家 targets.json 配置对应（hermes/hermes-cn 用下划线小写，dsh 用 Claude 标准名）。
PROBES = [
    ("hermes", "session_start", "pre_tool_call", "subagent_stop"),
    ("hermes-cn", "session_start", "pre_tool_call", "subagent_stop"),
    ("dsh", "SessionStart", "PreToolUse", "Stop"),
]


def _run(agent: str, event: str, sid: str, tool: str = "", tool_input: dict | None = None):
    payload: dict = {"hook_event_name": event, "session_id": f"{sid}-{agent}"}
    if tool:
        payload["tool_name"] = tool
        payload["tool_input"] = tool_input or {}
    p = subprocess.run([NODE, str(GUARD), "--agent", agent],
                       input=json.dumps(payload, ensure_ascii=False),
                       capture_output=True, text=True, encoding="utf-8", timeout=20)
    return p.returncode, (p.stdout or "").strip()


def main() -> int:
    if not GUARD.is_file():
        print(f"守卫脚本不在：{GUARD}")
        return 2
    before = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
    sid = f"probe-{int(time.time())}"
    ok = True
    print(f"探针 sid={sid}（契约级实测，非端到端）")
    for agent, ev_reset, ev_pre, ev_stop in PROBES:
        # 轮一：放行路径
        rc1, _ = _run(agent, ev_reset, sid)
        rc2, _ = _run(agent, ev_pre, sid, tool="hub_log_work",
                      tool_input={"content": "R63 闸门契约实测探针", "agent": agent})
        rc3, _ = _run(agent, ev_stop, sid)
        # 轮二：阻断路径（新会话：干了活没记大脑 → stop 必须 block）
        sid2 = f"{sid}-blk"
        _run(agent, ev_reset, sid2)
        _run(agent, ev_pre, sid2, tool="Read", tool_input={"file_path": "probe.txt"})
        rcb, outb = _run(agent, ev_stop, sid2)
        blocked = '"decision"' in outb and "block" in outb
        print(f"  {agent}: 放行轮 rc={rc1}/{rc2}/{rc3}｜阻断轮 rc={rcb} "
              f"decision={'block ✓' if blocked else '未出 block ✗'}")
        if not (rc1 == rc2 == rc3 == rcb == 0 and blocked):
            ok = False
    after = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
    new_lines = [ln for ln in after[len(before):].splitlines() if ln.strip()]
    print(f"\n守卫日志新增 {len(new_lines)} 行（归因核对）：")
    for ln in new_lines:
        print("  " + ln[:160])
    # 每个 agent 都必须有归因行，且两类关键证据齐全
    for agent, *_ in PROBES:
        mine = [ln for ln in new_lines if f"agent={agent} " in ln]
        has_reset = any("reset@" in ln for ln in mine)
        has_logged = any("marked logged" in ln for ln in mine)
        has_block = any("BLOCK" in ln for ln in mine)
        good = bool(mine) and has_reset and has_logged and has_block
        ok = ok and good
        print(f"[{'PASS' if good else 'FAIL'}] {agent}: 归因行 {len(mine)} 条"
              f"（reset={'✓' if has_reset else '✗'} marked={'✓' if has_logged else '✗'}"
              f" block={'✓' if has_block else '✗'}）")
    print("\nPROBE_ALL_PASS" if ok else "\nPROBE_HAS_FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
