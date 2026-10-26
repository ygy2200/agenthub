# -*- coding: utf-8 -*-
"""空库首启逐页审计（R193）：信号页 tab 栏拉伸、Agent 中心卡片区压住资产视图。

真实空根 920×600 实测：
  · 收件箱为空时 split 隐藏，多余高度摊给竖向 Minimum 的 SegmentedWidget，
    tab 栏被撑到 110px——背景胶囊、文字、指示条分三层；
  · 7 个 agent 在 920 宽排 3 行需 210px，父布局只给 161px，FlowLayout 第三行
    画出布局矩形，压住「资产视图」标题与搜索框。
"""
from types import SimpleNamespace

from PySide6.QtWidgets import QWidget

from helpers import boot_window, pump, settle


def _visible_gaps(box):
    """空态容器内相邻可见子控件的纵向间隔（R194 起空态改首尾伸缩项吸收富余高度，
    「容器被拉高」不再是问题，「内部被拉散」才是——直接量间隔）。"""
    kids = [c for c in box.children() if isinstance(c, QWidget) and c.isVisible()]
    rows = sorted(((c.geometry().y(), c.geometry().height()) for c in kids),
                  key=lambda r: r[0])
    return [rows[i + 1][0] - (rows[i][0] + rows[i][1]) for i in range(len(rows) - 1)]



def _agent(name, skills=1, mcps=1, mems=1, cfgs=1):
    return SimpleNamespace(
        name=name,
        home=f"C:\\Users\\y\\.{name.lower()}",
        skills=[None] * skills,
        mcps=[None] * mcps,
        memories=[None] * mems,
        configs=[None] * cfgs,
    )


def test_signal_tabs_keep_height_when_inbox_empty(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.signal_page)
        settle(qapp, max_s=2.0)
        p = w.signal_page
        assert p.seg.height() == 26, f"空收件箱把 tab 栏撑到 {p.seg.height()}px"
        assert all(it.geometry().bottom() < p.seg.height()
                   for it in p.seg.items.values()), "tab 文字跑出 tab 栏"
        assert p._gap_added, "空态未挂弹簧，剩余高度会再次摊给 tab 栏"
        hint = getattr(p.listw, "_empty_hint_widget", None)
        assert hint is not None and hint.isVisible()
        gaps = _visible_gaps(hint)
        assert all(0 <= g <= 12 for g in gaps), f"空态内部被拉散：{gaps}"
        assert hint.geometry().top() >= p.seg.geometry().bottom()

        brain.signal_send(hub_root, "zcode", "R193 广播测试", to_agent="")
        p.reload()
        settle(qapp, max_s=2.0)
        assert p.listw.count() == 1
        assert p.split.isVisible()
        assert not p._gap_added, "有信号时弹簧应移除，把高度还给 split"
        assert not hint.isVisible()
    finally:
        w.hide()


def test_hub_cards_scroll_instead_of_overlapping(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.hub_page)
        assert pump(qapp, lambda: hasattr(w.hub_page, "_reg"), 30), "agent 探测未完成"
        hp = w.hub_page
        hp.agents = [_agent(f"Agent{i}", 100, 3, 20, 2) for i in range(7)]
        hp._reg = {}
        hp.rebuild_cards()
        settle(qapp, 1.5)

        assert hp.cardsScroll.height() <= 140
        need = hp.cardsRow.heightForWidth(hp.cardsScroll.viewport().width())
        assert need > hp.cardsScroll.height(), f"7 张卡仅需 {need}px，未触发内滚"
        assert hp.viewHead.geometry().top() >= hp.cardsScroll.geometry().bottom(), \
            "卡片区仍压住资产视图"
        assert hp.stack.height() >= 60, "资产列表被挤到不可用"

        w.resize(1400, 900)
        settle(qapp, 1.5)
        assert hp.cardsScroll.verticalScrollBar().maximum() == 0, \
            "宽窗两行放得下时不应出现内滚"
    finally:
        w.hide()
