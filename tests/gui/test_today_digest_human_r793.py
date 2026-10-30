# -*- coding: utf-8 -*-
"""工作台日结建议说人话（R793）。

brain.team_digest 的 suggestions 是写给 agent 的修复指令，带 hub_ 工具名。
工作台直接把它拼进摘要/说明，920 走查里出现「先 hub_claim 认领；长期无人做就
hub_task_update 关单」。展示层翻译成人话，源数据保持 agent 可直接执行。
"""
from helpers import boot_window, settle

HINT = "先 hub_claim 认领；长期无人做就 hub_task_update 关单"


def _digest(hint):
    return {
        "flow": {"records": 3, "tasks_done": 1},
        "current": {"wip": {"stale_leases": 0, "approval_waiting": 0}, "open_total": 2},
        "risks": [{"kind": "unowned"}],
        "suggestions": [hint],
    }


def test_digest_suggestions_hide_tool_names(qapp, hub_root, monkeypatch):
    import brain

    brain.init_db(hub_root)
    monkeypatch.setattr(brain, "team_digest", lambda *a, **k: _digest(HINT))
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)
        line = page.digestLine.text()
        tip = page.digestLine.toolTip()
        assert "hub_" not in line and "hub_" not in tip, (line, tip)
        assert "认领" in line and "更新任务关单" in line, line
        assert "先 认领" not in line and "认领 认领" not in line, line
        assert "先 认领" not in tip and "认领 认领" not in tip, tip
    finally:
        w.hide()


def test_human_hint_keeps_unknown_tools_opaque():
    from pages.today_page import _human_hint

    out = _human_hint("用 hub_some_future_tool 处理")
    assert "hub_" not in out
    assert "对应操作" in out
