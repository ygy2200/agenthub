# -*- coding: utf-8 -*-
"""任务详情露出「技能要求 / 交付物 / 完成审批」（R808）。

这三个字段 agent 侧早就在用（认领比对、完成核验、审批放行），桌面详情却只字未提。
接手的人打开详情，第一眼就该知道：要什么能力、交什么产物、谁放行。
"""
from PySide6.QtWidgets import QWidget

from helpers import track_window


def _dlg(task):
    from uikit import TaskDetailDialog

    host = QWidget()
    track_window(host)
    return TaskDetailDialog(host, task)


def test_detail_states_skills_deliverables_and_approval(qapp):
    dlg = _dlg({
        "id": 7, "title": "R808", "status": "pending", "project": "P",
        "owner": "", "priority": 1, "deps": "", "detail": "", "due": "",
        "skills": "python, docs", "deliverables": "out/a.md\nhttps://example.com/x",
        "approval_required": 1, "approved_by": "",
    })
    try:
        line = dlg.reqLabel.text()
        assert "技能 python、docs" in line, line
        assert "交付物 2 项" in line, line
        assert "完成需审批（待批准）" in line, line
        tip = dlg.reqLabel.toolTip()
        assert "out/a.md" in tip and "https://example.com/x" in tip, tip
        assert "标记完成时逐条核验" in tip, tip
    finally:
        dlg.close()


def test_detail_without_requirements_is_honest(qapp):
    dlg = _dlg({
        "id": 8, "title": "R808 空要求", "status": "pending", "project": "",
        "owner": "", "priority": 2, "deps": "", "detail": "", "due": "",
        "skills": "", "deliverables": "", "approval_required": 0,
    })
    try:
        line = dlg.reqLabel.text()
        assert "无技能要求" in line and "无交付物" in line and "无需审批" in line, line
        assert "不核验产物" in dlg.reqLabel.toolTip()
    finally:
        dlg.close()


def test_approved_task_says_who_approved(qapp):
    dlg = _dlg({
        "id": 9, "title": "R808 已批", "status": "pending", "project": "",
        "owner": "", "priority": 1, "deps": "", "detail": "", "due": "",
        "skills": "", "deliverables": "", "approval_required": 1, "approved_by": "lead",
    })
    try:
        assert "已由 lead 批准" in dlg.reqLabel.text(), dlg.reqLabel.text()
    finally:
        dlg.close()
