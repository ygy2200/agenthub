# -*- coding: utf-8 -*-
"""R766：F5 刷新在扫描线程里崩了，界面毫无反应。

复现：把 `core.scan` 换成抛 RuntimeError，再 `w.refresh()`。修复前 Qt 只把异常打到
stderr（`Error calling Python override of QThread::run()`），done 信号不发射，
`window.apply` 永远不跑——用户看到的是「按了 F5 什么也没发生」，页面还留着旧内容。

修复（uikit.ScanWorker.run）：异常收成带 error 的空快照再 emit，由 window.apply
统一弹「扫描失败」InfoBar；旧内容不清（失败不该把上一份快照抹掉）。
"""
import sys
from pathlib import Path

from PySide6.QtWidgets import QLabel

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))

import core  # noqa: E402
from helpers import boot_window, pump, settle  # noqa: E402


def _texts(win):
    return [c.text() for c in win.findChildren(QLabel)
            if c.isVisible() and (c.text() or "").strip()]


def test_scan_crash_shows_failure_infobar(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        settle(qapp, 2.0)
        before = w.project_page.snap
        orig = core.scan

        def boom(root):
            raise RuntimeError("模拟扫描崩溃（r766）")

        core.scan = boom
        try:
            w.refresh()
            pump(qapp, lambda: any("扫描失败" in t for t in _texts(w)), 10)
        finally:
            core.scan = orig
        texts = _texts(w)
        assert any("扫描失败" in t for t in texts), texts[:20]
        assert any("模拟扫描崩溃" in t for t in texts), texts[:20]
        assert w.project_page.snap is before, "扫描失败不该把上一份内容清掉"
    finally:
        w._skip_close_hooks = True
        w.close()
