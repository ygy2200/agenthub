# -*- coding: utf-8 -*-
"""任务详情编辑前置依赖（R805）。

deps 原先只能由 agent 改：桌面能建任务、改说明和截止日，却调不动「谁先谁后」。
这里钉住三件事：对话框语法/自依赖拦截、保存后可回读、成环由 brain 拒绝且不落库。
"""
from helpers import boot_window, settle


def _insert(root, title, deps=""):
    import brain
    err, tid = brain.task_add(root, title=title, project="R805", priority=2,
                              deps=deps, agent="gui")
    assert not err, err
    return tid


def _select(page, tid):
    for i in range(page.listw.count()):
        t = page._item2task.get(id(page.listw.item(i)))
        if t and int(t["id"]) == int(tid):
            page.listw.setCurrentRow(i)
            return True
    return False


def test_deps_roundtrip_and_clear(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    first = _insert(hub_root, "R805 前置任务")
    second = _insert(hub_root, "R805 后置任务")
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)
        assert _select(page, second), "后置任务没进可认领队列"

        page.save_details("", "", str(first))
        settle(qapp, 0.5)
        row = brain.task_get(hub_root, second)
        assert row["deps"] == str(first)
        assert row["detail"] == "" and row["due"] == "", "只改依赖不该动说明/截止日"

        page.reload()
        settle(qapp, 0.5)
        assert _select(page, second)
        tip = page._task_tip(page._item2task[id(page.listw.currentItem())],
                             "ready", "R805 后置任务")
        assert f"前置任务：#{first}" in tip, "tooltip 应能回读前置任务号"

        page.save_details("", "", "")
        settle(qapp, 0.5)
        assert brain.task_get(hub_root, second)["deps"] == ""
    finally:
        w.hide()


def test_cycle_rejected_and_not_persisted(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    first = _insert(hub_root, "R805 环 A")
    second = _insert(hub_root, "R805 环 B", deps=str(first))
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)
        assert _select(page, first)
        # A 依赖 B，而 B 已依赖 A：成环必须被拒绝，且原值保持不变。
        page.save_details("", "", str(second))
        settle(qapp, 0.3)
        assert brain.task_get(hub_root, first)["deps"] == ""
    finally:
        w.hide()


def test_deps_dialog_validation(qapp):
    from PySide6.QtWidgets import QWidget
    from uikit import TaskDetailDialog

    host = QWidget()
    dlg = TaskDetailDialog(host, {
        "id": 7, "title": "R805 对话框", "status": "pending", "project": "P",
        "owner": "", "priority": 1, "deps": "3,4", "detail": "",
        "due": "",
    })
    try:
        assert dlg.depsEdit.text() == "3,4"
        assert dlg.yesButton.isEnabled()

        dlg.depsEdit.setText("7")
        assert not dlg.yesButton.isEnabled(), "自依赖不该能保存"
        assert "不能依赖自己" in dlg.hint.text()

        dlg.depsEdit.setText("abc")
        assert not dlg.yesButton.isEnabled(), "非数字任务号不该能保存"

        dlg.depsEdit.setText("#3, 4、3")
        assert dlg.yesButton.isEnabled(), "带 # 和中文分隔符应可识别"
        assert dlg.values()["deps"] == "3,4", "重复任务号应去重"

        dlg.depsEdit.setText("")
        assert dlg.values()["deps"] == "", "清空即取消依赖"
    finally:
        dlg.close()
        host.close()
