# -*- coding: utf-8 -*-
"""工作台任务详情 / 截止日编辑（R799）。

交接时最先被问的是「这单要什么、什么时候要」，桌面原来只能看标题。
选中任务后可补说明、改截止日（管理通道，别人持有的任务也能补），清空即取消。
"""
from helpers import boot_window, settle


def _insert(root, title, detail="", due=""):
    import brain
    err, tid = brain.task_add(root, title=title, project="R799", priority=2,
                              detail=detail, due=due, agent="gui")
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


def test_detail_and_due_roundtrip(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    tid = _insert(hub_root, "R799 补详情任务")
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)
        assert not page.detailBtn.isEnabled(), "没选中任务时详情按钮不该可点"
        assert _select(page, tid), "新任务没进可认领队列"
        assert page.detailBtn.isEnabled()

        page.save_details("验收：桌面能补说明并回读\n第二行", "2026-11-05")
        settle(qapp, 0.5)
        row = brain.task_get(hub_root, tid)
        assert "桌面能补说明" in row["detail"]
        assert row["due"] == "2026-11-05"

        page.reload()
        settle(qapp, 0.5)
        assert _select(page, tid)
        assert "截止:2026-11-05" in _row_text(page, tid)
        tip = page._task_tip(page._item2task[id(page.listw.currentItem())],
                             "ready", "R799 补详情任务")
        assert "桌面能补说明" in tip, "详情应能在行的 tooltip 里回读"

        # 清空即取消截止 / 清除详情（brain 认 "-"）
        page.save_details("", "")
        settle(qapp, 0.5)
        row = brain.task_get(hub_root, tid)
        assert row["detail"] == ""
        assert row["due"] == ""
        page.reload()
        settle(qapp, 0.5)
        assert _select(page, tid)
        assert "截止:" not in _row_text(page, tid)
    finally:
        w.hide()


def test_detail_editable_on_task_owned_by_another_agent(qapp, hub_root):
    """进行中任务归 zcode 持有：补说明走管理面，不该被持有者校验挡住。"""
    import brain

    brain.init_db(hub_root)
    tid = _insert(hub_root, "R799 别人持有的说明")
    err, _ = brain.task_claim(hub_root, tid, agent="zcode")
    assert not err, err
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)
        assert _select(page, tid), "进行中任务没进队列"
        page.save_details("交接口径：先跑回归再关单", "2026-11-06")
        settle(qapp, 0.5)
        row = brain.task_get(hub_root, tid)
        assert row["owner"] == "zcode", "管理面改详情不该换负责人"
        assert row["detail"] == "交接口径：先跑回归再关单"
        assert row["due"] == "2026-11-06"
    finally:
        w.hide()


def test_detail_dialog_prefills_and_validates_due(qapp):
    from PySide6.QtWidgets import QWidget
    from uikit import TaskDetailDialog

    host = QWidget()
    dlg = TaskDetailDialog(host, {
        "id": 7, "title": "R799 对话框", "status": "pending", "project": "P",
        "owner": "", "priority": 1, "deps": "3,4", "detail": "旧说明",
        "due": "2026-11-01",
    })
    try:
        assert dlg.detailEdit.toPlainText() == "旧说明"
        assert dlg.dueEdit.text() == "2026-11-01"
        assert "前置：3,4" in dlg.metaLabel.text()
        assert dlg.yesButton.isEnabled()

        dlg.dueEdit.setText("2026/11/01")
        assert not dlg.yesButton.isEnabled(), "非法截止日不该能保存"
        dlg.dueEdit.setText("")
        assert dlg.yesButton.isEnabled(), "清空截止日应允许保存"
        dlg.detailEdit.setPlainText("改过的说明")
        assert dlg.values() == {"detail": "改过的说明", "due": ""}
    finally:
        dlg.close()
        host.close()
