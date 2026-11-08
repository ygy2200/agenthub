# -*- coding: utf-8 -*-
r"""R1015：检索通道分账——「零命中」按通道拆，别把采集面样本算成白搜。

真库实测（2026-11-08，近 7 天）：
  prompt_recall 5616 / hub_search 86 / error_recall_miss 168（全零命中）/ error_recall 71
不拆账时 digest 报「检索 5973（零命中 190）」，读起来像 agent 搜了 5973 次、190 次白搜；
实际 190 条零命中里 168 条是「失败事件抽不出报错签名」的采集面样本，与关键词无关，
却和主动检索共用一条「换关键词/补记录」的建议——照着做只会白忙。

本文件锁：
1. 通道映射（agent / auto / error / miss / other）——未知工具归 other，不并进已有通道；
2. team_digest 的 searches_by_channel 分账数字正确；
3. 风险分流：主动检索零命中 → zero_hit_search；报错通道零命中 → error_recall_gap，
   且后者不再共用「换关键词」建议；
4. usage_stats.zero_hit_searches 带 tool 与通道标注。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402


def test_search_channel_mapping():
    assert brain._search_channel("hub_search") == "agent"
    assert brain._search_channel("hub_memory_read") == "agent"
    assert brain._search_channel("hub_skill_match") == "agent"
    assert brain._search_channel("prompt_recall") == "auto"
    assert brain._search_channel("recall_push") == "auto"
    assert brain._search_channel("error_recall") == "error"
    assert brain._search_channel("error_recall_miss") == "miss"
    assert brain._search_channel("hub_never_heard_of") == "other"
    assert brain._search_channel("") == "other"
    # 通道标签必须覆盖全部通道键（渲染层按标签取值）
    assert set(brain.SEARCH_CHANNEL_LABELS) >= set(brain.SEARCH_CHANNELS.values())


@pytest.fixture
def root_with_channels(tmp_path):
    root = str(tmp_path)
    brain.init_db(root)
    rows = [
        # tool, agent, query, hits
        ("hub_search", "workbuddy", "备份 恢复", 0),          # 主动白搜
        ("hub_search", "workbuddy", "备份 恢复", 0),          # 再来一次（反复）
        ("hub_memory_read", "qoder", "落盘 铁律", 0),          # 主动白搜
        ("hub_search", "qoder", "钩子 注入", 3),              # 主动命中
        ("prompt_recall", "workbuddy", "AgentHub", 0),        # 自动回忆没命中
        ("error_recall", "workbuddy", "syntaxerror", 0),      # 报错真查了库没命中
        ("error_recall_miss", "qoder", "(no_signature)", 0),  # 采集面：抽不出签名
        ("error_recall_miss", "qoder", "(no_signature)", 0),
        ("error_recall_miss", "workbuddy", "permissionerror", 0),
        ("hub_unknown_tool", "x", "zzz", 0),                  # 未知工具 → other
    ]
    with brain.db_conn(root) as conn:
        for tool, ag, q, h in rows:
            brain._search_insert(conn, tool, q, h, ag)
    return root


def test_team_digest_splits_search_channels(root_with_channels):
    d = brain.team_digest(root_with_channels, days=7)
    ch = d["flow"]["searches_by_channel"]
    assert ch["agent"]["searches"] == 4      # hub_search×3 + hub_memory_read×1
    assert ch["agent"]["zero"] == 3          # 其中 hub_search 命中 3 条不算零
    assert ch["auto"]["searches"] == 1 and ch["auto"]["zero"] == 1
    assert ch["error"]["searches"] == 1 and ch["error"]["zero"] == 1
    assert ch["miss"]["searches"] == 3 and ch["miss"]["zero"] == 3
    assert ch["other"]["searches"] == 1 and ch["other"]["zero"] == 1
    assert d["flow"]["searches"] == 10
    assert d["flow"]["zero_hit_searches"] == 9


def test_digest_risk_separates_error_channel(root_with_channels):
    d = brain.team_digest(root_with_channels, days=7)
    kinds = {r["kind"]: r for r in d["risks"]}
    assert "zero_hit_search" in kinds, "主动检索白搜必须单独点名"
    assert kinds["zero_hit_search"]["count"] == 3
    assert "error_recall_gap" in kinds, "报错通道零命中必须单独点名"
    assert kinds["error_recall_gap"]["count"] == 4      # error 1 + miss 3
    # 关键：报错通道的建议不得再是「换关键词」——那是主动检索的处置
    hint = kinds["error_recall_gap"]["hint"]
    assert "换关键词" not in hint
    assert "采集面" in hint and "failure_samples" in hint
    assert "换关键词" in kinds["zero_hit_search"]["hint"]


def test_usage_stats_zero_hit_carries_channel(root_with_channels):
    st = brain.usage_stats(root_with_channels, days=7)
    rows = st["zero_hit_searches"]
    assert rows, "零命中应有样本"
    by_ch = {}
    for r in rows:
        by_ch.setdefault(r["_channel"], []).append(r)
        assert "tool" in r
    assert "agent" in by_ch and "miss" in by_ch
    # 同一 query 不同工具分行（不再被 GROUP BY query 合并成一条误导样本）
    miss = by_ch["miss"][0]
    assert miss["tool"].startswith("error_recall")


def test_zero_hit_risk_absent_when_no_zero(root_with_channels):
    with brain.db_conn(root_with_channels) as conn:
        conn.execute("UPDATE searches SET hits=3")
    d = brain.team_digest(root_with_channels, days=7)
    kinds = {r["kind"] for r in d["risks"]}
    assert "zero_hit_search" not in kinds
    assert "error_recall_gap" not in kinds
