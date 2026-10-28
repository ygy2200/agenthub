# -*- coding: utf-8 -*-
r"""看板抬头不把「取到的条数」当总数（R271）。

hub_tasks 默认 limit=50，抬头写「任务 50 条」——库里 60 条时调用方以为只有 50。
守四件事：超限时报总数并说明「这里列 N 条」；未超限不出现「共…」措辞；
limit 可调大（≤200）；长标题截断带省略号。
"""
import agenthub_mcp as m
import brain


def _seed(root, n, title=None):
    brain.init_db(root)
    for i in range(n):
        err, tid = brain.task_add(root, title or f"任务{i}", project="P-项目")
        assert not err, err


def test_over_limit_reports_total(hub_root):
    _seed(hub_root, 60)
    out = m.call_tool("hub_tasks", {}, hub_root)
    assert "共 60 条任务，这里列 50 条" in out, out[:300]
    assert "任务 50 条（" not in out


def test_under_limit_has_no_total_phrase(hub_root):
    _seed(hub_root, 3)
    out = m.call_tool("hub_tasks", {}, hub_root)
    assert "任务 3 条（" in out, out[:300]
    assert "这里列" not in out


def test_limit_raised_matches_total(hub_root):
    _seed(hub_root, 60)
    out = m.call_tool("hub_tasks", {"limit": 200}, hub_root)
    assert "这里列" not in out, out[:300]
    out60 = m.call_tool("hub_tasks", {"limit": 60}, hub_root)
    assert "这里列" not in out60, out60[:300]


def test_status_filter_counts_filtered_total(hub_root):
    _seed(hub_root, 5)
    for i in range(3):
        brain.task_update(hub_root, i + 1, status="done")
    out = m.call_tool("hub_tasks", {"status": "done"}, hub_root)
    assert "任务 3 条（" in out, out[:300]
    assert "5 条" not in out.split("\n")[0]


def test_long_title_gets_ellipsis(hub_root):
    _seed(hub_root, 1, title="长" * 80)
    out = m.call_tool("hub_tasks", {}, hub_root)
    assert "…" in out, out[:400]
    assert ("长" * 61) not in out
