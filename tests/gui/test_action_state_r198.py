# -*- coding: utf-8 -*-
"""动作按钮跟随选中态（R198）。

真实空库 920×600 走查：工作台队列为空时「标记完成」恒亮，点下去只弹「先选中一个任务」；
编排中心首次进入五条流程一条都没选中，「跑一次」等按钮全灰，看起来像功能不可用。
"""
from helpers import boot_window, pump, settle


def _insert_task(root, title):
    import brain
    err, tid = brain.task_add(root, title=title, project="R198", priority=2, agent="zcode")
    assert not err, err
    return tid


def test_today_done_button_follows_selection(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.today_page)
        tp = w.today_page
        tp.reload()
        settle(qapp, 1.0)
        assert tp.listw.count() == 0
        assert not tp.doneBtn.isEnabled(), "空队列时「标记完成」不该可点"

        tid = _insert_task(hub_root, "R198 待办任务")
        brain.task_update(hub_root, tid, status="pending", agent="zcode")
        tp.reload()
        settle(qapp, 1.0)
        # 待办（未认领）也不该能标记完成——只有「进行中」可以
        tp.doneBtn.setEnabled(True)  # 先人为点亮，确认 reload 会把它收回
        tp.reload()
        settle(qapp, 0.8)
        assert not tp.doneBtn.isEnabled(), "未认领任务不该能直接标记完成"

        brain.task_claim(hub_root, tid, agent="zcode")
        tp.reload()
        settle(qapp, 1.0)
        row = next((i for i in range(tp.listw.count())
                    if tp._item2task.get(id(tp.listw.item(i)))), None)
        assert row is not None, "进行中任务没进列表"
        tp.listw.setCurrentRow(row)
        settle(qapp, 0.5)
        assert tp.doneBtn.isEnabled(), "选中进行中任务后「标记完成」应可点"
    finally:
        w.hide()


def test_playbook_selects_first_flow(qapp, hub_root, monkeypatch):
    import brain

    brain.init_db(hub_root)
    rows = [{"name": "daily", "title": "每日巡检", "builtin": 1, "n_steps": 10,
             "n_done": 0, "last_status": None},
            {"name": "release", "title": "发布全流程", "builtin": 1, "n_steps": 7,
             "n_done": 0, "last_status": None}]
    monkeypatch.setattr(brain, "playbook_list", lambda *a, **k: rows)
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.playbook_page)
        settle(qapp, 1.2)
        pb = w.playbook_page
        pb.seg.setCurrentItem("flows")
        pb.reload()
        settle(qapp, 1.0)
        assert pb.listw.count() == 2
        assert pb.listw.currentRow() == 0, "有流程却没选中首条，动作按钮会全灰"
        assert pb.cur_flow == "daily", "选中首条后应记进 cur_flow"
        assert pb.runBtn.isEnabled(), "选中流程后「跑一次」应可点"
    finally:
        w.hide()
