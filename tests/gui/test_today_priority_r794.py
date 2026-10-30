# -*- coding: utf-8 -*-
"""工作台优先级编辑（R794）。

R792 之前桌面端只能建任务/标完成/取消重试，改优先级还得绕去让 agent 调
hub_task_update；而优先级正是「今天先做哪件」的排序依据。这里走管理通道直改，
按钮跟随选中态，改完列表按新优先级重排。
"""
from helpers import boot_window, settle


def _insert(root, title, priority=2):
    import brain
    err, tid = brain.task_add(root, title=title, project="R794",
                              priority=priority, agent="gui")
    assert not err, err
    return tid


def _select(page, tid):
    for i in range(page.listw.count()):
        t = page._item2task.get(id(page.listw.item(i)))
        if t and int(t["id"]) == int(tid):
            page.listw.setCurrentRow(i)
            return True
    return False


def _row_text(page, tid):
    for i in range(page.listw.count()):
        t = page._item2task.get(id(page.listw.item(i)))
        if t and int(t["id"]) == int(tid):
            return page.listw.item(i).text()
    return ""


def test_priority_updates_and_requeues(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    tid = _insert(hub_root, "R794 调优先级任务", priority=2)
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)
        assert _select(page, tid), "新任务没进可认领队列"
        assert page.prioBtn.isEnabled()
        assert "[P2]" in _row_text(page, tid)

        page.set_priority(0)
        settle(qapp, 0.5)
        assert int(brain.task_get(hub_root, tid)["priority"]) == 0
        page.reload()
        settle(qapp, 0.5)
        assert "[P0]" in _row_text(page, tid), "改完优先级后列表应显示并重排"

        page.set_priority(0)
        settle(qapp, 0.2)
        assert int(brain.task_get(hub_root, tid)["priority"]) == 0, "同档位不该重复写库"
    finally:
        w.hide()


def test_priority_works_on_task_owned_by_another_agent(qapp, hub_root):
    """进行中的任务归 zcode 持有：桌面改优先级走管理面，不该被持有者校验挡住。"""
    import brain

    brain.init_db(hub_root)
    tid = _insert(hub_root, "R794 别人持有的任务", priority=2)
    err, _ = brain.task_claim(hub_root, tid, agent="zcode")
    assert not err, err
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)
        assert _select(page, tid), "进行中任务没进队列"
        page.set_priority(1)
        settle(qapp, 0.5)
        row = brain.task_get(hub_root, tid)
        assert int(row["priority"]) == 1, "管理面改优先级被持有者校验挡住了"
        assert row["owner"] == "zcode", "改优先级不该动负责人"
    finally:
        w.hide()


def test_priority_disabled_without_selection(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    _insert(hub_root, "R794 无选中任务")
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)
        page.listw.clearSelection()
        page.listw.setCurrentItem(None)
        page._sync_actions()
        assert not page.prioBtn.isEnabled(), "没选中任务时优先级按钮不该可点"
    finally:
        w.hide()
