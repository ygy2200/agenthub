# -*- coding: utf-8 -*-
"""B019 回归：用例自建的裸宿主窗口必须登记并在用例边界真正销毁。

根因不是 400ms 首刷定时器（R199 已挂父），而是 `Page(win)` 的 parent 默认 None：
页面成了无父顶层控件，`win.close()` 只管宿主，页面连同其 qfw 控件留在进程里。
qfw 的 styleSheetManager 对每个注册控件持强引用，`deleteLater` 之外的路径都收不走；
单进程连开多个窗口累到 ~100 个注册控件时随机 access violation（实测本文件
test_connect_page_summary 连跑 2/3 崩）。本用例钉住：登记 + 挂父后，
cleanup_windows 能把页面 C++ 对象真正析构，qfw 注册表回到基线。
"""
from PySide6.QtWidgets import QWidget

import helpers
from helpers import settle


def _adapter(name):
    return {"agent": name, "mcp": {"path": f"/fake/{name}.json", "layout": "standard"},
            "instr_path": f"/fake/{name}.instr"}


def test_tracked_host_destroys_page_and_deregisters_styles(qapp, hub_root, monkeypatch):
    import core
    from pages.connect_page import ConnectPage
    from qfluentwidgets.common.style_sheet import styleSheetManager

    monkeypatch.setattr(core, "load_agent_adapters", lambda root="": [_adapter("a")])
    monkeypatch.setattr(core, "mcp_has_agenthub", lambda path, layout="": True)
    monkeypatch.setattr(core, "bootstrap_status", lambda path: True)
    monkeypatch.setattr(core, "deploy_status", lambda *a, **k: {
        "python": "D:/python.exe", "server": "/fake/server", "state": "ready",
        "diff": [], "absent": [], "same": 11})

    before = len(styleSheetManager.widgets)
    win = QWidget()
    win.root = str(hub_root)
    helpers.track_window(win)
    page = ConnectPage(win, win)
    settle(qapp, max_s=0.6)
    page.refresh_status()
    settle(qapp, max_s=0.3)
    assert len(styleSheetManager.widgets) > before, "页面控件没进 qfw 注册表，用例前提失效"

    helpers.cleanup_windows(qapp)

    try:
        page.objectName()
    except RuntimeError:
        pass
    else:
        raise AssertionError("登记的宿主窗口清理后，页面 C++ 对象仍存活（B019 会复发）")
    assert len(styleSheetManager.widgets) == before, \
        f"qfw 注册表没回落：清理前 {before}，清理后 {len(styleSheetManager.widgets)}"
