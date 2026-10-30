# -*- coding: utf-8 -*-
"""gui 层：时间线筛选行与记录密度（R789）。

两个已截图确认的版式问题：
  1) 「筛选」标题独占一行，agent/项目 控件在下一行被推到最右——标题与控件脱节；
  2) 记录不足一屏时，布局把富余高度摊给日期组及其间距，日期标题与首条记录被
     拉开 70~100px、组与组之间空出上百像素，3 条记录也显得松散。
"""
from PySide6.QtCore import QPoint

from helpers import boot_window, settle


def _seed(brain, root):
    for i in range(3):
        brain.add_record(root, "P0", "zcode", f"2026-01-0{3 - i}", f"r{i}", "c", "hub")


def test_filter_head_and_controls_share_one_row(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    _seed(brain, hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(1280, 820)
        w.switchTo(w.timeline_page)
        settle(qapp, max_s=1.5)
        page = w.timeline_page
        page.layout().activate()
        settle(qapp, max_s=0.5)

        lay = page.layout()
        filter_row = None
        for i in range(lay.count()):
            sub = lay.itemAt(i).layout()
            if sub is None:
                continue
            if any(sub.itemAt(j).widget() is page.agentFilter
                   for j in range(sub.count())):
                filter_row = sub
                break
        assert filter_row is not None, "筛选控件必须直接放在页面的一个水平行里"
        heads = [wid.title_label.text() for wid in
                 (filter_row.itemAt(j).widget() for j in range(filter_row.count()))
                 if wid is not None and hasattr(wid, "title_label")]
        assert heads == ["筛选"], f"「筛选」标题应与控件同一行（实际 {heads}）"
        head_y = [wid.mapTo(page, QPoint(0, 0)).y() for wid in
                  (filter_row.itemAt(j).widget() for j in range(filter_row.count()))
                  if wid is not None and hasattr(wid, "title_label")]
        combo_y = page.agentFilter.mapTo(page, QPoint(0, 0)).y()
        assert abs(head_y[0] + 10 - (combo_y + page.agentFilter.height() // 2)) <= 6, \
            "筛选标题与下拉框应垂直居中对齐"
    finally:
        w.hide()


def test_groups_stay_compact_when_content_short(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    _seed(brain, hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(1280, 820)
        w.switchTo(w.timeline_page)
        settle(qapp, max_s=1.5)
        page = w.timeline_page
        page.layout().activate()
        page.scroll.widget().layout().activate()
        settle(qapp, max_s=0.5)

        groups = [page.box.itemAt(i).widget() for i in range(page.box.count())
                  if page.box.itemAt(i).widget() is not None
                  and page.box.itemAt(i).widget().layout() is not None]
        assert len(groups) == 3, "三条不同日期的记录应形成三个日期组"
        tops = [g.mapTo(page.scroll.widget(), QPoint(0, 0)).y() for g in groups]
        assert tops[0] <= 20, f"首组应贴近列表顶部，实际 y={tops[0]}"
        gaps = [tops[i + 1] - (tops[i] + groups[i].height()) for i in range(2)]
        assert max(gaps) <= 24, f"日期组间距应保持紧凑（实际 {gaps}）"

        for g in groups:
            gl = g.layout()
            head = gl.itemAt(0).widget()
            badge = gl.itemAt(1).layout().itemAt(0).widget()
            gap = badge.mapTo(g, QPoint(0, 0)).y() - head.geometry().bottom()
            assert gap <= 16, f"日期标题与首条记录间距 {gap}px，应保持紧凑（≤16px）"
    finally:
        w.hide()
