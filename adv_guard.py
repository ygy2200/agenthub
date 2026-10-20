# -*- coding: utf-8 -*-
"""agent_guard.mjs 对抗回归（R8）：守卫原生剥壳，不再依赖每家适配器。

覆盖四种调用形态（直连 / mcp_call / hub_call_tool / 套娃）、记大脑判定
（content 非空且非 dry_run 才算）、Stop 阻断与次数上限、present_files 闸门、
会话隔离、逃生门。运行：python adv_guard.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FAILED = []
GUARD = Path(__file__).resolve().parent / "agent_guard.mjs"


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def node_exe() -> str:
    cands = [os.environ.get("AGENTHUB_NODE", ""), shutil.which("node") or "",
             r"D:\node\node.exe", r"C:\Program Files\nodejs\node.exe"]
    for c in cands:
        if c and Path(c).is_file():
            return c
    return ""


class Runner:
    def __init__(self, node: str, home: str):
        self.node = node
        self.env = {**os.environ, "USERPROFILE": home, "HOME": home}

    def run(self, ev: dict, extra_env: dict | None = None) -> tuple[str, int]:
        env = {**self.env, **(extra_env or {})}
        p = subprocess.run([self.node, str(GUARD), "--agent", "guardtest"],
                           input=json.dumps(ev, ensure_ascii=False), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           env=env, timeout=20)
        return p.stdout.strip(), p.returncode

    def start(self, sid: str) -> str:
        return self.run({"hook_event_name": "SessionStart", "session_id": sid})[0]

    def pre(self, sid: str, name: str, ti: dict | None = None) -> str:
        return self.run({"hook_event_name": "PreToolUse", "session_id": sid,
                         "tool_name": name, "tool_input": ti or {}})[0]

    def stop(self, sid: str, extra_env: dict | None = None) -> str:
        return self.run({"hook_event_name": "Stop", "session_id": sid}, extra_env)[0]


def is_block(out: str) -> bool:
    try:
        return json.loads(out).get("decision") == "block"
    except ValueError:
        return False


def main():
    node = node_exe()
    if not node:
        print("node 不在 PATH/默认路径，无法运行守卫回归（设 AGENTHUB_NODE 重试）")
        sys.exit(1)
    home = tempfile.mkdtemp(prefix="agenthub_guard_")
    r = Runner(node, home)

    # ① 三种「写大脑」形态都必须被识别（否则 Stop 会误拦）
    check("SessionStart 静默", r.start("s1") == "")
    check("mcp_call 套 mcp__agenthub__hub_memory_write 计入 logged",
          r.pre("s1", "mcp_call", {"toolName": "mcp__agenthub__hub_memory_write",
                                   "arguments": {"content": "知识", "kind": "fact"}}) == ""
          and r.stop("s1") == "")
    r.start("s2")
    check("mcp__agenthub__hub_call_tool 套 hub_log_work 计入 logged",
          r.pre("s2", "mcp__agenthub__hub_call_tool",
                {"toolName": "hub_log_work", "arguments": {"content": "记录"}}) == ""
          and r.stop("s2") == "")
    r.start("s3")
    check("两层套娃（mcp_call → hub_call_tool → hub_memory_write）也能剥开",
          r.pre("s3", "mcp_call", {"toolName": "hub_call_tool",
                                   "arguments": {"toolName": "hub_memory_write",
                                                 "arguments": {"content": "套娃"}}}) == ""
          and r.stop("s3") == "")

    # ② 普通工具 = 干活；未记大脑必须阻断
    r.start("s4")
    r.pre("s4", "read_file", {"path": "a"})
    check("干过活未记大脑 → Stop 阻断", is_block(r.stop("s4")))
    r.pre("s4", "mcp__agenthub__hub_log_work", {"content": "补记"})
    check("补记后同一会话放行（状态是活的）", r.stop("s4") == "")
    r.start("s5")
    r.pre("s5", "read_file", {})
    r.pre("s5", "mcp__agenthub__hub_log_work", {"content": "z", "dry_run": True})
    check("dry_run 不翻牌（仍阻断）", is_block(r.stop("s5")))
    r.start("s6")
    r.pre("s6", "read_file", {})
    r.pre("s6", "hub_call_tool", {"toolName": "hub_log_work", "arguments": {"content": "   "}})
    check("空 content 不翻牌（仍阻断）", is_block(r.stop("s6")))
    r.start("s7")
    r.pre("s7", "mcp_call", {"toolName": "hub_search", "arguments": {"query": "q"}})
    check("剥壳后非写大脑工具只算干活（不误判 logged）", is_block(r.stop("s7")))

    # ③ present_files 交付闸门
    r.start("s8")
    deny = r.pre("s8", "present_files", {"paths": ["a"]})
    try:
        denied = json.loads(deny)["hookSpecificOutput"]["permissionDecision"] == "deny"
    except (ValueError, KeyError):
        denied = False
    check("present_files 未记大脑 → deny", denied, deny[:160])
    r.pre("s8", "mcp__agenthub__hub_memory_write", {"content": "交付前记忆"})
    check("记过大脑 → present_files 放行", r.pre("s8", "present_files", {"paths": ["a"]}) == "")

    # ④ 阻断上限（2 次后放行，防死循环）+ 会话隔离
    r.start("s9")
    r.pre("s9", "read_file", {})
    b1, b2, b3 = is_block(r.stop("s9")), is_block(r.stop("s9")), r.stop("s9")
    check("Stop 最多阻断 2 次（第3次放行防死循环）", b1 and b2 and b3 == "", f"{b1}/{b2}/{b3!r}")
    r.start("s10")
    r.pre("s10", "read_file", {})
    r.start("s11")
    r.pre("s11", "mcp__agenthub__hub_log_work", {"content": "另一个会话"})
    check("会话隔离（s11 已记不影响 s10 的闸门）", is_block(r.stop("s10")) and r.stop("s11") == "")

    # ⑤ 逃生门 + 证据落盘（只在临时 HOME，不碰真实日志）
    r.start("s12")
    r.pre("s12", "read_file", {})
    check("ASU_SKIP_BRAINLOG=1 逃生门静默放行",
          r.stop("s12", {"ASU_SKIP_BRAINLOG": "1"}) == "")
    state_file = Path(home) / ".workbuddy" / "state" / "agent-guard.json"
    log_file = Path(home) / ".workbuddy" / "logs" / "agent-guard.log"
    check("守卫状态与日志落在预期位置（不污染真实 HOME）",
          state_file.is_file() and log_file.is_file())
    log_text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
    check("日志记录 agent 归因", "agent=guardtest" in log_text)
    check("日志保留嵌套形态（可追查是哪条调用翻的牌）", "→hub_" in log_text)

    shutil.rmtree(home, ignore_errors=True)
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("GUARD ALL PASS")


if __name__ == "__main__":
    main()
