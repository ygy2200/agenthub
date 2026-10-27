# -*- coding: utf-8 -*-
"""R205 单实例守门：第二实例不得再起一套窗口/热键/DB 写入。"""
import uuid

from PySide6.QtWidgets import QApplication


def test_claim_single_instance_second_call_returns_false(monkeypatch):
    app = QApplication.instance() or QApplication([])
    import window
    # 独立管道名，避免撞上本机真实运行的 AgentHub
    monkeypatch.setenv("USERNAME", "pytest-" + uuid.uuid4().hex[:8])
    assert window._claim_single_instance(app) is True
    server = window._SINGLE_INSTANCE
    try:
        assert window._claim_single_instance(app) is False
    finally:
        if server is not None:
            server.close()
        window._SINGLE_INSTANCE = None


def test_second_instance_branch_has_no_modal_dialog():
    """R215 双开冒烟实测：弹 QMessageBox 会让第二个进程卡住等点确定，应静默退出。"""
    import inspect
    import window
    src = inspect.getsource(window.run_gui)
    branch = src.split("if not _claim_single_instance(app):", 1)[1].split("_setup_app_font", 1)[0]
    assert "QMessageBox" not in branch, "第二实例分支不应弹模态对话框"
    assert "return" in branch, "第二实例分支应直接返回退出"
