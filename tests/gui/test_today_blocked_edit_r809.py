# -*- coding: utf-8 -*-
"""详情可补阻塞原因（R809）。

R806 让阻塞原因在行内可见，但当时仍只能由 agent 写。等人拍板/等环境这类原因
恰恰是用户在桌面最清楚的；只对阻塞任务开放、必填，解除阻塞仍交给状态流转。
"""
import brain

from helpers import boot_window, settle, track_window


def _add(root, **kw):
    kw.setdefault("project", "R809")
    kw.setdefault("agent", "gui")
    err, tid = brain.task_add(root, **kw)
    assert not err, err
    return tid


def test_blocked_dialog_requires_reason(qapp):
    from PySide6.QtWidgets import QWidget
    from uikit import TaskDetailDialog

    host = QWidget()
    track_window(host)
    dlg = TaskDetailDialog(host, {
        "id": 9, "title": "R809", "status": "blocked", "project": "",
        "owner": "", "priority": 1, "deps": "", "detail": "", "due": "",
        "blocked_by": "等用户拍板",
    })
    try:
        assert dlg.blockedEdit is not None
        assert dlg.blockedEdit.text() == "等用户拍板"
        dlg.blockedEdit.setText("等真机复测")
        assert dlg.values()["blocked_by"] == "等真机复测"

        dlg.blockedEdit.setText("")
        assert not dlg.yesButton.isEnabled(), "阻塞任务不许把原因清空"
        assert "必须写明原因" in dlg.hint.text(), dlg.hint.text()
    finally:
        dlg.close()


def test_pending_dialog_does_not_offer_blocked_reason(qapp):
    from PySide6.QtWidgets import QWidget
    from uikit import TaskDetailDialog

    host = QWidget()
    track_window(host)
    dlg = TaskDetailDialog(host, {
        "id": 10, "title": "R809 待办", "status": "pending", "project": "",
        "owner": "", "priority": 1, "deps": "", "detail": "", "due": "",
    })
    try:
        assert dlg.blockedEdit is None, "非阻塞任务不该出现阻塞原因输入"
        assert "blocked_by" not in dlg.values()
    finally:
        dlg.close()


def test_save_reason_keeps_status_blocked(qapp, hub_root):
    brain.init_db(hub_root)
    tid = _add(hub_root, title="R809 等人拍板", blocked_by="用户决策")

    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)
        item = next(page.listw.item(i) for i in range(page.listw.count())
                    if int(page._item2task.get(id(page.listw.item(i)), {}).get("id") or 0) == tid)
        page.listw.setCurrentItem(item)

        page.save_details("", "", None, "等用户确认预算")
        settle(qapp, 0.5)
        row = brain.task_get(hub_root, tid)
        assert row["blocked_by"] == "等用户确认预算", row
        assert row["status"] == "blocked", "改原因不应顺带改状态"
    finally:
        w.hide()
