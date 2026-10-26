# -*- coding: utf-8 -*-
"""Agent 中心纵向预算（R195）：卡片区不得压住资产视图，且高度必须对齐整行。

真实复现（920×600 + 微软雅黑）：页头/说明/卡片区/资产视图/编辑条/预览各块最小高度
合计 565 > 页高 551，QVBoxLayout 压不下去时不是裁字，而是把「资产视图」标题与搜索框
叠到卡片区上；同时「两行上限」按 2*row_h+12 截断会在第二行卡片中间切断。

断言的 invariant（与字体无关）：
  · 卡片区底部 ≤ 资产视图标题顶部（绝对坐标，跨父级也算）；
  · 卡片区高度 = k 行 + (k-1) 个行间距，k ∈ {1,2}；
  · 资产视图（stack）仍有可用高度（≥60）。
"""
from PySide6.QtCore import QPoint

from helpers import boot_window, pump, settle


def _agent(name, skills=1, mcps=1, mems=1, cfgs=1):
    from types import SimpleNamespace

    return SimpleNamespace(
        name=name,
        home=f"C:\\Users\\y\\.{name.lower()}",
        skills=[None] * skills,
        mcps=[None] * mcps,
        memories=[None] * mems,
        configs=[None] * cfgs,
    )


def _abs_top_bottom(page, w):
    tl = w.mapTo(page, QPoint(0, 0))
    return tl.y(), tl.y() + w.height()


def _assert_no_overlap(page, label):
    cards_bottom = _abs_top_bottom(page, page.cardsScroll)[1]
    head_top = _abs_top_bottom(page, page.viewHead)[0]
    assert head_top >= cards_bottom, f"{label}：资产视图({head_top}) 压住卡片区底({cards_bottom})"
    assert page.cardsScroll.height() > 0


def _row_metrics(page):
    row_h = 0
    for i in range(page.cardsRow.count()):
        w = page.cardsRow.itemAt(i).widget()
        if w is not None:
            row_h = max(row_h, w.sizeHint().height())
    row_h = row_h or 62
    gap = getattr(page.cardsRow, "_v", 12)
    return row_h, gap


def test_cards_never_overlap_asset_view(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        assert pump(qapp, lambda: bool(getattr(w.hub_page, "agents", None)), 30), "agent 探测未完成"
        hp = w.hub_page
        hp.agents = [_agent(f"Agent{i}", 100, 3, 20, 2) for i in range(7)]
        hp._reg = {}
        for size in ((920, 600), (920, 470), (1280, 820)):
            w.resize(*size)
            w.switchTo(hp)
            hp.rebuild_cards()
            settle(qapp, 0.8)
            _assert_no_overlap(hp, f"{size}")
            row_h, gap = _row_metrics(hp)
            h = hp.cardsScroll.height()
            rows = (h + gap) // (row_h + gap)
            assert 1 <= rows <= 2, f"{size}：卡片区 {h}px 不是 1–2 整行（行高 {row_h}/间距 {gap}）"
            assert h == rows * row_h + (rows - 1) * gap, \
                f"{size}：卡片区高度 {h}px 没有对齐整行（{rows} 行 = {rows * row_h + (rows - 1) * gap}px）"
            assert hp.stack.height() >= 60, f"{size}：资产视图被挤到 {hp.stack.height()}px"
    finally:
        w.hide()
