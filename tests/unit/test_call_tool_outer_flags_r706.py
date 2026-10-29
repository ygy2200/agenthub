# -*- coding: utf-8 -*-
"""R706：hub_call_tool 外层 dry_run/confirm 必须生效，不能静默丢弃。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _brain(tmp_path):
    import brain
    root = str(tmp_path / "hub")
    Path(root).mkdir()
    brain.init_db(root)
    return root


def test_outer_dry_run_prevents_write(tmp_path):
    import agenthub_mcp as m
    root = _brain(tmp_path)
    out = m.call_tool("hub_call_tool", {
        "name": "hub_task_add",
        "arguments": {"title": "不该落库的任务"},
        "dry_run": True,
    }, root)
    assert "未执行" in out and "dry_run" in out, out
    listing = m.call_tool("hub_tasks", {}, root)
    assert "不该落库的任务" not in listing, listing


def test_outer_confirm_false_blocks_write(tmp_path):
    import agenthub_mcp as m
    root = _brain(tmp_path)
    out = m.call_tool("hub_call_tool", {
        "name": "hub_task_add",
        "arguments": {"title": "等人工确认"},
        "confirm": False,
    }, root)
    assert "未执行" in out and "confirm" in out, out
    assert "等人工确认" not in m.call_tool("hub_tasks", {}, root)


def test_inner_flag_wins_over_outer(tmp_path):
    import agenthub_mcp as m
    root = _brain(tmp_path)
    out = m.call_tool("hub_call_tool", {
        "name": "hub_task_add",
        "arguments": {"title": "内层说了算", "dry_run": False},
        "dry_run": True,
    }, root)
    # 内层 dry_run=False 胜出：必须真的落库，且不能回 dry_run 的拦截话术。
    assert "未执行" not in out and "任务已建" in out, out
    assert "内层说了算" in m.call_tool("hub_tasks", {}, root)
