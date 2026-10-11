# -*- coding: utf-8 -*-
"""gui 层：真实窗口启动 + 全页渲染冒烟（无头渲染）。"""
import time

from helpers import boot_window, pump


def test_window_all_pages_render(qapp, hub_root):
    import actions
    import brain
    import tokens
    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 30), \
        "扫描未完成（apply 未跑）"

    failed = []
    for attr, title, *_rest in actions.PAGES:
        page = getattr(w, attr, None)
        assert page is not None, f"{attr} 不存在"
        w.switchTo(page)
        for _ in range(6):
            qapp.processEvents()
            time.sleep(0.02)
        pm = page.grab()
        if pm.isNull() or pm.width() < 100 or pm.height() < 100:
            failed.append(title)
    assert not failed, f"页面渲染失败：{failed}"


def test_components_page_has_sections(qapp, hub_root):
    """P2-3c：组件预览页必须渲染出各区块（控件数下界做粗守卫）。"""
    from PySide6.QtWidgets import QWidget as _QW

    import brain
    import tokens
    brain.init_db(hub_root)
    tokens.apply_theme("light")
    w = boot_window(qapp, hub_root)
    comp = w.components_page
    w.switchTo(comp)
    for _ in range(8):
        qapp.processEvents()
        time.sleep(0.02)
    n = len(comp.findChildren(_QW))
    assert n > 40, f"组件预览页控件过少（{n}），疑似区块缺失"
    pm = comp.grab()
    assert not pm.isNull() and pm.height() > 300
