# -*- coding: utf-8 -*-
"""R1138 / B138：在不动语义契约的前提下把 admin 面压回预算内。

外部取证见 output/迭代依据-R1135-B138外部取证-2026-11-12.md：
MCP 官方 / tsecurity / ai-cost-estimator / lmversity 一致主张压缩叙述性长句，
而本仓 R17 实测「描述缺示例 / 使用时机 / 必填参数」掉约 18 点准确率。
二者可调和——可砍的是 `RNN：` 历史编号与权限句的重复括注，
必须留的是 --agent 锁身份、manager 权限边界、末尾 `例：`。

三条钉：真实源码回到预算内 / 关键语义一个不少 / desc_quality 仍通过。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ops  # noqa: E402


def test_real_source_back_within_budget():
    r = ops.run_check("profile_surface")
    assert r["ok"] is True, r["summary"]


def test_compression_keeps_contract_signals():
    import agenthub_mcp as m
    d = {t["name"]: t.get("description", "") for t in m.TOOLS}
    for name in ("hub_memory_supersede", "hub_export", "hub_lock"):
        assert "例：" in d[name], f"{name} 的示例被砍（R17）"
    for name in ("hub_undo", "hub_claim", "hub_todo_done", "hub_checkpoint",
                 "hub_signal_send", "hub_lock"):
        assert "--agent" in d[name], f"{name} 的锁身份语义被砍"
    assert "manager" in d["hub_memory_supersede"], "权限边界被砍"
    assert all(len(d[n]) >= 200 for n in
               ("hub_memory_supersede", "hub_export", "hub_lock", "hub_task_update")), (
        "最长几条被整体砍短，疑似误伤使用时机说明")


def test_desc_quality_still_passes():
    assert ops.run_check("desc_quality")["ok"] is True
