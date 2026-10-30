# -*- coding: utf-8 -*-
"""工作台任务取消/重试（R792）。

R785 之后桌面端能建任务，但建错了只能让 agent 来撤；取消与重试都要求写原因，
桌面入口沿用同一条管理通道（agent 空传＝管理面），并跟随选中态启停。
"""
from helpers import boot_window, settle


def _insert(root, title):
    import brain
    err, tid = brain.task_add(root, title=title, project="R792", priority=2, agent="gui")
    assert not err, err
    return tid


def _select(page, tid):
    import brain  # noqa: F401
    for i in range(page.listw.count()):
        t = page._item2task.get(id(page.listw.item(i)))
        if t and int(t["id"]) == int(tid):
            page.listw.setCurrentRow(i)
            return True
    return False


def test_cancel_requires_reason_and_leaves_queue(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    tid = _insert(hub_root, "R792 待取消任务")
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)
        assert _select(page, tid), "新任务没进可认领队列"
        assert page.cancelBtn.isEnabled()
        assert not page.retryBtn.isEnabled(), "没失败过的待办不该能重试"

        page.cancel_task("需求撤回，改由后续任务承接")
        settle(qapp, 0.5)
        row = brain.task_get(hub_root, tid)
        assert row["status"] == "cancelled"
        assert "需求撤回" in row["cancel_reason"]
        page.reload()
        settle(qapp, 0.5)
        texts = "\n".join(page.listw.item(i).text() for i in range(page.listw.count()))
        assert "R792 待取消任务" not in texts, "取消后不该继续留在前沿队列"
    finally:
        w.hide()


def test_retry_records_reason_and_requeues(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    tid = _insert(hub_root, "R792 待重试任务")
    err, _ = brain.task_claim(hub_root, tid, agent="zcode")
    assert not err, err
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)
        assert _select(page, tid), "进行中任务没进队列"
        assert page.retryBtn.isEnabled(), "进行中任务应可记录失败并重试"

        page.retry_task("网络超时，已确认代理后重试")
        settle(qapp, 0.5)
        row = brain.task_get(hub_root, tid)
        assert row["status"] == "pending", "重试应放回队列"
        assert int(row["attempts"]) == 1
        assert "网络超时" in (row["last_error"] or "")
        page.reload()
        settle(qapp, 0.5)
        texts = "\n".join(page.listw.item(i).text() for i in range(page.listw.count()))
        assert "R792 待重试任务" in texts, "退避中的任务仍要可见（标注多久后可再试）"
        assert "分钟后可再试" in texts
    finally:
        w.hide()


def test_reason_dialog_blocks_empty(qapp, hub_root):
    from uikit import ReasonDialog
    from PySide6.QtWidgets import QWidget

    host = QWidget()
    dlg = ReasonDialog(host, "取消任务 #1", "必须写原因", "为什么", "取消任务")
    try:
        assert not dlg.yesButton.isEnabled(), "原因为空时不该能提交"
        dlg.reasonEdit.setPlainText("需求已撤")
        assert dlg.yesButton.isEnabled()
        assert dlg.reason() == "需求已撤"
    finally:
        dlg.close()
        host.close()
