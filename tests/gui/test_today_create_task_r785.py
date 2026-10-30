# -*- coding: utf-8 -*-
"""工作台桌面建任务（R785）。

空态原先只教「让 agent 说一句『新建任务 …』」：没开 agent 的用户在工作台无路可走，
而这一页正是「今天该干什么」的第一落点。本轮补桌面入口（管理面署名 gui），
并把空态 CTA 指回同一个动作。
"""
from qfluentwidgets import PushButton

from helpers import boot_window, settle


def test_empty_state_offers_create_task(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)
        assert page.listw.count() == 0
        hint = getattr(page.listw, "_empty_hint_widget", None)
        assert hint is not None, "空队列必须有引导"
        assert hint.isVisible()
        texts = [b.text() for b in hint.findChildren(PushButton)]
        assert any("新建任务" in t for t in texts), f"空态缺建任务入口：{texts}"
        assert page.newBtn.isEnabled(), "新建任务不依赖选中，应恒可用"
    finally:
        w.hide()


def test_create_task_lands_in_ready_queue(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        err, tid = page.create_task("R785 桌面建的任务", "工作台-入口", 1, "2026-12-31")
        assert err == "", err
        assert tid > 0
        row = brain.task_get(hub_root, tid)
        assert row["status"] == "pending"
        assert row["created_by"] == "gui", "桌面建任务应署名管理面"
        assert int(row["priority"]) == 1
        page.reload()
        settle(qapp, 0.8)
        all_text = "\n".join(page.listw.item(i).text() for i in range(page.listw.count()))
        assert "R785 桌面建的任务" in all_text, "新任务没进可认领队列"
    finally:
        w.hide()


def test_new_task_dialog_validates(qapp, hub_root):
    from uikit import NewTaskDialog
    from PySide6.QtWidgets import QWidget

    host = QWidget()
    dlg = NewTaskDialog(host, ["甲-项目"])
    try:
        assert not dlg.yesButton.isEnabled(), "标题为空时不该可创建"
        dlg.titleEdit.setText("有标题了")
        assert dlg.yesButton.isEnabled()
        dlg.dueEdit.setText("2026/12/31")
        assert not dlg.yesButton.isEnabled(), "截止日格式不对要拦住"
        dlg.dueEdit.setText("2026-12-31")
        assert dlg.yesButton.isEnabled()
        v = dlg.values()
        assert v["title"] == "有标题了" and v["due"] == "2026-12-31"
        assert v["priority"] == 2
    finally:
        dlg.close()
