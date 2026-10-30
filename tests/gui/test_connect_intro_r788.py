# -*- coding: utf-8 -*-
"""gui 层：接入中心顶部先讲「目的 + 三步」，长能力清单收进可展开细节（R788）。

原先首屏是一段 150+ 字的协议/能力/操作混合说明，用户读不出先做什么；
本用例锁定：主说明足够短、三步顺序可见、工具数动态、能力清单默认折叠且可展开。
"""
from PySide6.QtWidgets import QWidget

from helpers import track_window


def _build(qapp, hub_root, monkeypatch):
    import core
    from pages.connect_page import ConnectPage

    monkeypatch.setattr(core, "load_agent_adapters", lambda root="": [
        {"agent": "a", "mcp": {"path": "/fake/a.json", "layout": "standard"},
         "instr_path": "/fake/a.instr"}])
    monkeypatch.setattr(core, "mcp_has_agenthub", lambda path, layout="": True)
    monkeypatch.setattr(core, "bootstrap_status", lambda path: True)
    monkeypatch.setattr(core, "deploy_status", lambda *a, **k: {
        "python": "D:/python.exe", "server": "/fake/server", "state": "ready",
        "diff": [], "absent": [], "same": 11})

    win = QWidget()
    win.root = str(hub_root)
    track_window(win)
    page = ConnectPage(win, win)
    page._bootTimer.stop()
    return win, page


def test_connect_intro_is_short_and_action_ordered(qapp, hub_root, monkeypatch):
    import agenthub_mcp

    win, page = _build(qapp, hub_root, monkeypatch)
    try:
        text = page.introLabel.text()
        assert len(text) <= 120, f"首屏说明过长（{len(text)} 字）：{text}"
        i1, i2, i3 = text.index("①"), text.index("②"), text.index("③")
        assert i1 < i2 < i3, "三步必须按 接入 → 引导 → 开工报到 的顺序呈现"
        assert f"{len(agenthub_mcp.TOOLS)} 个工具" in text
        assert "hub_" not in text and "CLI" not in text
    finally:
        win.close()


def test_connect_capability_list_collapsed_by_default(qapp, hub_root, monkeypatch):
    win, page = _build(qapp, hub_root, monkeypatch)
    try:
        assert page.introDetail.isHidden(), "能力清单默认应收起，首屏只留目的与三步"
        assert not page.moreBtn.isChecked()
        assert "公用记忆" in page.introDetail.text()
        assert "撤销" in page.introDetail.text()

        page.moreBtn.click()
        assert not page.introDetail.isHidden(), "点「能力清单」后应展开细节"
        assert page.moreBtn.isChecked()

        page.moreBtn.click()
        assert page.introDetail.isHidden(), "再次点击应收起"
    finally:
        win.close()
