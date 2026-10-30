# -*- coding: utf-8 -*-
"""接入中心「全部就绪」折叠成一行（R199）。

920×600 实测：6 张 96px 卡片只露出 1.5 张，第二张从标题就被切断，像渲染坏了。
就绪状态下默认折叠成一行摘要 + 「展开详情」；出现任何缺口强制展开。
"""
from PySide6.QtWidgets import QWidget

from helpers import settle, track_window


def _adapter(name):
    return {"agent": name, "mcp": {"path": f"/fake/{name}.json", "layout": "standard"},
            "instr_path": f"/fake/{name}.instr"}


def _state():
    return {"python": "D:/python.exe", "server": "/fake/server", "state": "ready",
            "diff": [], "absent": [], "same": 11}


def _page(qapp, hub_root, monkeypatch, adapters, connected=(), booted=()):
    import core
    from pages.connect_page import ConnectPage

    monkeypatch.setattr(core, "load_agent_adapters", lambda root="": list(adapters))
    monkeypatch.setattr(core, "mcp_has_agenthub", lambda path, layout="": path in set(connected))
    monkeypatch.setattr(core, "bootstrap_status", lambda path: path in set(booted))
    monkeypatch.setattr(core, "deploy_status", lambda *a, **k: dict(_state()))
    win = QWidget()
    win.root = str(hub_root)
    track_window(win)          # B019：不登记的裸窗口不销毁，qfw 注册表会把整页留住
    page = ConnectPage(win, win)
    settle(qapp, max_s=0.6)
    page.refresh_status()
    return win, page


def test_all_ready_collapses_to_one_line(qapp, hub_root, monkeypatch):
    rows = [_adapter("a"), _adapter("b"), _adapter("c")]
    win, page = _page(qapp, hub_root, monkeypatch, rows,
                      connected=("/fake/a.json", "/fake/b.json", "/fake/c.json"),
                      booted=("/fake/a.instr", "/fake/b.instr", "/fake/c.instr"))
    try:
        assert page.collapsedRow.isVisibleTo(page), "全部就绪时应折叠成一行"
        assert page.collapsedLabel.text().startswith("3 个 agent 全部已接入")
        assert page.scroll.maximumHeight() == 0, "折叠时卡片区应收起"
        page.expandBtn.click()
        settle(qapp, 0.4)
        assert page.scroll.maximumHeight() > 0, "点「展开详情」应放出卡片区"
        assert not page.collapsedRow.isVisibleTo(page)
        page.refresh_status()                       # 用户展开后刷新不得自己合上
        settle(qapp, 0.4)
        assert page.scroll.maximumHeight() > 0, "刷新把手动展开的列表又折叠了"
    finally:
        win.close()


def test_gap_forces_expand(qapp, hub_root, monkeypatch):
    rows = [_adapter("a"), _adapter("b")]
    win, page = _page(qapp, hub_root, monkeypatch, rows,
                      connected=("/fake/a.json", "/fake/b.json"),
                      booted=("/fake/a.instr",))       # b 没注入引导 → 有缺口
    try:
        assert not page.collapsedRow.isVisibleTo(page), "有缺口时必须展开，不能让缺口藏在折叠里"
        assert page.scroll.maximumHeight() > 0
    finally:
        win.close()
