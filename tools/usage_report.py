# -*- coding: utf-8 -*-
"""D6 评估工具：工具使用覆盖报告——「从未被调用」的工具到底该怎么处置。

背景：MCP 生产实践一致点名「从未被调用的工具通常是描述不清或与其他工具重叠」
（technologyonthe.net / itential）。AgentHub 69 个工具里 48 个零调用，看起来
是该治理了。但先要问一句**数据可不可信**——本脚本第一件事就是回答这个：

  ① **留痕覆盖面**：三源（op_audit 写 / searches 检索 / tool_calls 调用）
     实际能覆盖多少工具？在 R6 之前 tool_calls 只记 4 个能力型只读工具，
     于是「48/69 从未被调用」里绝大部分是**留痕盲区的假象**，不是真没人用。
     拿假象做治理会把在用工具当僵尸下线——这是本脚本存在的首要理由。
  ② **零调用清单 + 分类建议**：不是所有零调用都该下线。分成三类：
     - keep-escape：逃生舱/发现类（hub_help/hub_search_tools/hub_call_tool/
       hub_undo…）——低频是**设计使然**，它们是「藏起≠禁用」与出问题时的兜底；
     - keep-governance：治理/体检类（hub_sentry/hub_duplicates/hub_health…）
       ——由人或定时任务偶尔调用，低频正常；
     - review：真正需要审查的（描述不清/与别家重叠/功能已被取代的候选）。
  ③ **边界诚实标注**：本清单只反映 **MCP 面**的使用。GUI 直接调 brain.py、
     不走 MCP，其使用**不在任何一张表内**——因此零调用绝不等于没人用，
     只是「MCP 面上没有证据」。
  ④ **历史偏差标注（R67，2026-10-23）**：经 hub_call_tool 多路复用转发的调用，
     该日期前只记得到多路复用本身、内层工具不留痕——历史数据**低估**多路复用
     路由的工具（qoder 全家走此路）；R67 起已补内层留痕，新数据可信。

用法：python tools/usage_report.py [大脑根目录]
      加 --json 输出机器可读（供后续自动化治理消费）
"""
from __future__ import annotations

import _console  # noqa: F401  统一 UTF-8 控制台（R222）：GBK 管道下 print("✓") 会崩

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agenthub_mcp as mcp  # noqa: E402

# 分类：低频是设计使然，**不因零调用而下线**的依据
KEEP_ESCAPE = {          # 逃生舱/发现面：藏起≠禁用，出问题时的兜底路径
    "hub_help", "hub_search_tools", "hub_call_tool", "hub_undo", "hub_audit",
}
KEEP_GOVERNANCE = {      # 治理/体检面：由人或定时任务偶尔触发，低频正常
    "hub_sentry", "hub_duplicates", "hub_health", "hub_skill_stats",
    "hub_bootstrap_status", "hub_list_mcps", "hub_list_agents", "hub_ops_list",
    "hub_flush_index", "hub_quarantine", "hub_user_profile", "hub_metacognition",
    "hub_history", "hub_decision_review", "hub_consolidate", "hub_view",
}


def collect_usage(root: str) -> dict:
    """三源并集 → {工具名: 次数}。任何一个源缺失都 fail-open（不炸）。"""
    con = sqlite3.connect(os.path.join(root, "_hub", "brain.db"))
    used: dict = {}
    try:
        for table, col in (("op_audit", "tool"), ("tool_calls", "tool"), ("searches", "tool")):
            try:
                for (name, n) in con.execute(f"SELECT {col}, COUNT(*) FROM {table} GROUP BY {col}"):
                    if name:
                        used[str(name).strip()] = used.get(str(name).strip(), 0) + int(n)
            except sqlite3.Error:
                continue      # 表不存在/列不存在：该源整块跳过，不因缺表而误判为零
    finally:
        con.close()
    return used


def classify(name: str) -> tuple:
    """返回 (分类, 一句话理由)。"""
    if name in KEEP_ESCAPE:
        return "keep-escape", "逃生舱/发现面——低频是设计使然，藏起≠禁用"
    if name in KEEP_GOVERNANCE:
        return "keep-governance", "治理/体检面——由人或定时任务偶尔触发"
    return "review", "需要审查：描述不清 / 与别家重叠 / 功能已被取代的候选"


def overlap_pairs(threshold: float = 0.30) -> list:
    """④ 工具**描述重叠**候选——「从未被调用」的另一个原因（研究点名：与其他工具重叠）。

    复用大脑自己的 bigram 分词（`brain._tokens`）算描述 Jaccard：两条描述说的是一回事
    时，agent 会分不清该调哪个，结果是两个都不调（或随机挑一个调错）。
    **只报不裁**：是否合并/改描述是设计决策，脚本负责把候选摆出来。
    """
    import brain  # noqa: PLC0415  延迟导入：只在本函数用到
    items = []
    for t in mcp.TOOLS:
        # 描述 + 工具名都参与（名字本身常是主要区分信号，如 hub_signal_read vs hub_signals）
        text = f"{t.get('name', '')} {t.get('description', '')}"
        toks = brain._tokens(text)
        if toks:
            items.append((t["name"], toks))
    out = []
    for i in range(len(items)):
        na, ta = items[i]
        for j in range(i + 1, len(items)):
            nb, tb = items[j]
            inter = len(ta & tb)
            if not inter:
                continue
            sim = inter / len(ta | tb)
            if sim >= threshold:
                out.append((round(sim, 2), na, nb))
    out.sort(reverse=True)
    return out


def main() -> int:
    root = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    if not root:
        root = os.environ.get("AGENTHUB_ROOT") or str(
            Path.home() / ".agenthub" / "root")
        # 缺省根取自本机配置（与 cli 同口径），不存在时给出明确指引而非静默空跑
        cfg = Path.home() / ".agenthub" / "config.json"
        if cfg.exists():
            try:
                root = json.loads(cfg.read_text(encoding="utf-8")).get("root", root)
            except Exception:  # noqa: BLE001
                pass
    if not Path(root).exists():
        print(f"错误：大脑根目录不存在：{root}（传一个路径，或设 AGENTHUB_ROOT）")
        return 2

    used = collect_usage(root)
    tools = [t["name"] for t in mcp.TOOLS]
    zero = [n for n in tools if used.get(n, 0) == 0]
    buckets = {"keep-escape": [], "keep-governance": [], "review": []}
    for n in zero:
        kind, _ = classify(n)
        buckets[kind].append(n)

    print("=== 工具使用覆盖报告（D6）===")
    print(f"工具总数 {len(tools)}｜有使用证据 {len(tools) - len(zero)}｜零调用 {len(zero)}")
    print()
    print("⚠ 数据边界：本表只反映 **MCP 面**的调用。GUI 直接调 brain.py、不走 MCP，"
          "其使用不在任何一张表内——零调用 ≠ 没人用，只是「MCP 面上没有证据」。")
    print("⚠ 历史偏差（R67，2026-10-23 前）：经 hub_call_tool 多路复用转发的调用，"
          "此前只记得到多路复用本身、内层工具不留痕——该日期前的数据**低估**多路复用"
          "路由的工具（qoder 全家走此路），零调用清单在该日期前不可全信；R67 起已补内层留痕。")
    print()
    for kind in ("keep-escape", "keep-governance", "review"):
        label = {"keep-escape": "① 保留·逃生舱/发现面",
                 "keep-governance": "② 保留·治理/体检面",
                 "review": "③ 待审查（真正需要人看一眼的）"}[kind]
        why = classify(buckets[kind][0])[1] if buckets[kind] else ""
        print(f"{label}：{len(buckets[kind])} 个 —— {why}")
        for n in buckets[kind]:
            print(f"    {n}")
        print()

    # ④ 描述重叠候选：不止「从没被调用」，还有「两个都说同一件事，于是都不敢调」
    pairs = overlap_pairs()
    print(f"④ 描述重叠候选（Jaccard ≥ 0.30）：{len(pairs)} 对 —— "
          "说的是一回事时 agent 会分不清该调哪个，结果是两个都不调")
    for sim, a, b in pairs[:12]:
        print(f"    {sim}  {a} ‖ {b}")
    if len(pairs) > 12:
        print(f"    …（还有 {len(pairs) - 12} 对）")
    print()

    if "--json" in sys.argv:
        print(json.dumps({"total": len(tools), "used": len(tools) - len(zero),
                          "zero": zero, "buckets": buckets,
                          "overlap": [[s, a, b] for s, a, b in pairs]},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
