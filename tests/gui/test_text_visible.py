# -*- coding: utf-8 -*-
"""gui 层：可见文本不得被布局压到 0 宽（"文字消失"守卫，视觉打磨 P4 / 2026-10-15）。

这一类缺陷**不崩、不报错，只是字没了**，肉眼也很难定位（背景还在、行还在）：
  · 2026-10-11：总览"项目名"列被行内 stretch 挤到 0 宽 → 完全不可见（已修，keepWidth）
  · 2026-10-15：缩放回归抓到 Agent 中心整张 agent 卡片 15 个标签 0 宽
    （flex_label 的水平 Ignored 策略会让 sizeHint 失效，在"父布局按 sizeHint
    分配宽度"的场景里直接塌成 0）

守卫口径：任一页面上，可见（isVisible）且有文字的 QLabel，宽度不得 ≤1px。
keepWidth 属性表示该标签自管宽度（已知例外），不计入。
"""
from helpers import boot_window, pump

MIN_TEXT_LABELS_OK = 0   # 允许的 0 宽可见文本数量（0 = 一个都不许）


def _collapsed(page):
    from PySide6.QtWidgets import QLabel
    out = []
    for lb in page.findChildren(QLabel):
        if not lb.text() or not lb.isVisible():
            continue
        if lb.property("keepWidth"):
            continue
        if lb.width() <= 1:
            out.append(lb.text()[:24])
    return out


def test_no_visible_text_collapses_to_zero_width(qapp, hub_root):
    """18 个页面里，不允许出现"可见但 0 宽"的文本标签。"""
    import actions
    import brain
    import tokens
    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 30), \
        "扫描未完成"
    # 让 agent 探测/环境采集等后台线程也落一落（Agent 中心的卡片是异步填的）
    assert pump(qapp, lambda: bool(getattr(w.hub_page, "agents", None)), 20), \
        "Agent 中心未探测到 agent"

    bad = {}
    for attr, title, *_rest in actions.PAGES:
        page = actions.page_of(w, attr)
        if page is None:
            continue
        w.switchTo(page)
        for _ in range(8):
            qapp.processEvents()
        gone = _collapsed(page)
        if len(gone) > MIN_TEXT_LABELS_OK:
            bad[title] = gone[:5]
    assert not bad, f"可见文本被压到 0 宽（文字消失）：{bad}"
