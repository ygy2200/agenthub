# -*- coding: utf-8 -*-
"""C7 评估工具：工具数与默认 profile 切片的数据驱动测量。

背景：外部研究（wavespeed·MCP 生产环境实战 / hamzashabbir 实测）称模型
**过 ~40 个工具开始退化**（选择准确率下降、上下文膨胀），而 AgentHub 现有
69 个工具、默认 profile=admin 全量。本脚本测量三件事，供「默认 profile
是否该切片」的决策使用：

  ① 三个 profile 各自的工具数 / tools/list 序列化体积 / 估算 token 成本；
  ② 真实大脑库里 6 个 agent 实际用过的工具（op_audit 写操作 + searches
     检索 + tool_calls 只读能力三源并集，与 report 的使用判定同口径）；
  ③ 交叉：实际用过的工具里，若默认改成 worker 会有哪些被藏起来。

用法：python tools/profile_eval.py [大脑根目录]
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

import agenthub_mcp as m  # noqa: E402

DEFAULT_ROOT = r"D:\agent资料\zcode项目记录"


def est_tokens(text: str) -> int:
    """粗估 token：CJK 约 1 字 1 token，ASCII 约 4 字符 1 token（与主流 BPE 量级一致）。"""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff" or c in "，。；：、（）「」【】")
    other = len(text) - cjk
    return cjk + other // 4


def profile_stats():
    rows = []
    orig = m.SERVER_PROFILE
    try:
        for name in ("reader", "worker", "admin"):
            m.SERVER_PROFILE = name
            tools = m.visible_tools()
            payload = json.dumps({"tools": tools}, ensure_ascii=False)
            rows.append((name, len(tools), len(payload), est_tokens(payload)))
    finally:
        m.SERVER_PROFILE = orig
    return rows


def used_tools(root: str):
    db = Path(root) / "_hub" / "brain.db"
    if not db.is_file():
        return {}, " brain.db 不存在"
    con = sqlite3.connect(str(db))
    per_agent = {}
    for table, col in (("op_audit", "tool"), ("searches", "tool"), ("tool_calls", "tool")):
        try:
            for agent, tool in con.execute(f"SELECT agent, {col} FROM {table}"):
                per_agent.setdefault(agent or "-", set()).add(tool)
        except sqlite3.Error:
            pass
    con.close()
    return per_agent, ""


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROOT
    print("=== ① profile 切片测量（tools/list 被动面） ===")
    print(f"{'profile':<8} {'工具数':>6} {'序列化字符':>12} {'估算token':>10}")
    for name, n, chars, tok in profile_stats():
        print(f"{name:<8} {n:>6} {chars:>12} {tok:>10}")

    print("\n=== ② 真实使用（三源并集，与 report 口径一致） ===")
    per_agent, note = used_tools(root)
    real_names = {t["name"] for t in m.TOOLS}
    pseudo = set()
    all_used = set()
    for agent in sorted(per_agent):
        tools = per_agent[agent]
        pseudo |= {t for t in tools if t not in real_names}
        all_used |= {t for t in tools if t in real_names}
        shown = "、".join(sorted(t for t in tools if t in real_names)) or "（无真实工具调用）"
        print(f"  {agent:<10} 用过 {len(tools & real_names):>2} 个：{shown}")
    if pseudo:
        print(f"  （伪工具名，非 MCP 工具，忽略：{'、'.join(sorted(pseudo))}）")
    print(f"  合计去重：{len(all_used)} 个工具被真实调用过（总工具 {len(m.TOOLS)}）{note}")

    print("\n=== ③ 交叉：默认改 worker 会藏起哪些「被真实用过的」工具 ===")
    m.SERVER_PROFILE = "worker"
    worker_visible = {t["name"] for t in m.visible_tools()}
    m.SERVER_PROFILE = "admin"
    hidden_used = sorted(all_used - worker_visible)
    if hidden_used:
        print(f"  ⚠ {len(hidden_used)} 个：{'、'.join(hidden_used)}")
        for t in hidden_used:
            users = sorted(a for a, ts in per_agent.items() if t in ts)
            print(f"    - {t}（使用者：{'、'.join(users)}）")
    else:
        print("  ✓ 无——真实用过的工具全在 worker 切片内")
    print("\n注：token 为量级估算（CJK≈1/字，ASCII≈4/字符）；"
          "tools/list 每会话一次，是「被动上下文成本」，调用次数另计。")


if __name__ == "__main__":
    main()
