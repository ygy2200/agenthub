# -*- coding: utf-8 -*-
"""工作台「显示更多」（R803）。

摘要里写着可认领 25，列表却只出现 20 条，组头还写「可认领（20）」——
总数与列表对不上，用户会以为任务丢了，也没有任何加载入口。
"""
from helpers import boot_window, settle


def _add(root, n):
    import brain
    for i in range(n):
        err, _tid = brain.task_add(root, title=f"R803 任务 {i:02d}", project="R803",
                                   priority=2, agent="gui")
        assert not err, err


def _heads(page):
    return [page.listw.item(i).text() for i in range(page.listw.count())
            if page.listw.item(i).text().startswith("──")]


def test_truncated_group_discloses_and_loads_more(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    _add(hub_root, 25)
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)

        assert "可认领 25" in page.sumLabel.text()
        heads = _heads(page)
        assert any("可认领（25，显示前 20）" in t for t in heads), heads
        assert len(page._item2task) == 20, "首屏应只渲染 20 条"
        assert not page.moreBtn.isHidden(), "有截断时必须给「显示更多」出口"
        assert page.moreBtn.isEnabled()
        assert "5" in page.moreBtn.toolTip(), page.moreBtn.toolTip()

        page.moreBtn.click()
        settle(qapp, 0.8)
        assert page._page_limit == 50
        heads = _heads(page)
        assert any("可认领（25）" in t for t in heads), heads
        assert len(page._item2task) == 25, "展开后应看到全部 25 条"
        assert page.moreBtn.isHidden(), "没有截断时按钮收起，避免常驻噪音"
    finally:
        w.hide()


def test_small_group_has_no_more_button(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    _add(hub_root, 3)
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.5)
        heads = _heads(page)
        assert any("可认领（3）" in t for t in heads), heads
        assert page.moreBtn.isHidden()
    finally:
        w.hide()
