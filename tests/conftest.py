# -*- coding: utf-8 -*-
"""测试三层共享装置（P2-3d）：unit（纯 core）/ gui（无头渲染）/ e2e（协议·进程级）。"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# GUI 层无头渲染（必须在任何 QApplication 创建前生效）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 测试进程不触网：语义模型走本地缓存；"不带离线变量的生产路径"由 adv_brain.t_emb_local_first 覆盖
os.environ.setdefault("HF_HUB_OFFLINE", "1")


@pytest.fixture
def hub_root(tmp_path, monkeypatch):
    """独立临时大脑根。patch core.get_root/save_config，防测试触碰真实库与用户配置。"""
    import core
    root = tmp_path / "hub-root"
    root.mkdir()
    monkeypatch.setattr(core, "get_root", lambda: str(root))
    monkeypatch.setattr(core, "save_config", lambda *a, **k: None)
    return str(root)


@pytest.fixture(autouse=True)
def _flush_qt_deletes():
    """每个用例结束后强制冲刷 Qt 的 DeferredDelete 队列（R199）。

    背景：GUI 用例在同一进程里连开多个窗口时，`deleteLater` 的控件要等事件循环
    空闲才析构；下一个用例已经建了新窗口，旧控件却在新窗口的事件分发中析构 →
    实测 20–30s 处随机 `Windows fatal exception: access violation`（同一份代码
    有时 4 passed、有时崩）。flush + gc 把析构时机钉在用例边界，代价可忽略。
    """
    yield
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QCoreApplication, QEvent
        app = QApplication.instance()
        if app is not None:
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
        import gc
        gc.collect()
    except Exception:  # noqa: BLE001 Qt 未启动/已销毁时不该影响用例结论
        pass
