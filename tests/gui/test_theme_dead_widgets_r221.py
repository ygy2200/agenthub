# -*- coding: utf-8 -*-
"""R221：整窗销毁后切主题不得崩（qfw 样式注册表残留死包装）。"""
from PySide6.QtCore import QCoreApplication, QEvent


def test_apply_theme_after_window_teardown(qapp, hub_root):
    """窗口整棵子树销毁后立刻切主题。

    病史：套件里前一个用例关窗后，后一个用例 tokens.apply_theme 抛
    `SystemError: longobject.c bad argument to internal function`（实测落在
    QFile.open 读 qss）。根因是 qfw 给每个注册控件挂的 destroyed → deregister
    回调，销毁时对已失效的 Shiboken 包装做 hash 抛 SystemError，管理器和
    解释器异常状态都被留脏。修法：删除前注销整棵子树的样式注册（helpers
    .cleanup_windows），apply_theme 前再兜底清一次死条目。
    """
    from helpers import boot_window, settle
    import tokens

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    settle(qapp, max_s=1.0)      # 让窗口构造后 QTimer.singleShot(0) 的导航收起先落地
    w.close()
    w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()

    tokens.apply_theme("light")
    tokens.apply_theme("dark")
