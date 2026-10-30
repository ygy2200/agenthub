# -*- coding: utf-8 -*-
"""工作台截止紧迫度（R800）。

业界任务工具把「已过期」当第一眼信号（Microsoft Planner 过期日期标红、
Jira 日历用红色+时钟表示 overdue、Todoist 高优先标红）。AgentHub 原来只把
due 原样拼成「截止:2026-10-20」，过没过期要用户自己算。这里钉住：
逾期/今天到期直接写进行内，未来日期保持原样，坏日期不崩。
"""
from datetime import date, timedelta

from helpers import boot_window, settle


def _insert(root, title, due):
    import brain
    err, tid = brain.task_add(root, title=title, project="R800", priority=2,
                              due=due, agent="gui")
    assert not err, err
    return tid


def _find_row(page, tid):
    for i in range(page.listw.count()):
        t = page._item2task.get(id(page.listw.item(i)))
        if t and int(t["id"]) == int(tid):
            return page.listw.item(i), t
    return None, None


def test_due_urgency_in_rows_and_tooltips(qapp, hub_root):
    import brain

    today = date.today()
    d_yday = (today - timedelta(days=1)).isoformat()
    d_today = today.isoformat()
    d_future = (today + timedelta(days=3)).isoformat()
    bad_due = "2026-13-40"

    brain.init_db(hub_root)
    t_yday = _insert(hub_root, "R800 逾期任务", d_yday)
    t_today = _insert(hub_root, "R800 今天到期", d_today)
    t_future = _insert(hub_root, "R800 未来任务", d_future)
    t_bad = _insert(hub_root, "R800 坏日期", bad_due)
    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)

        it, task = _find_row(page, t_yday)
        assert it is not None, "逾期任务没进队列"
        assert "逾期 1 天" in it.text() and d_yday in it.text(), it.text()
        tip = page._task_tip(task, "ready", task["title"])
        assert "已逾期 1 天" in tip
        assert "改期：选中任务 → 详情" in tip, tip

        it, task = _find_row(page, t_today)
        assert "今天到期" in it.text(), it.text()
        assert "今天到期" in page._task_tip(task, "ready", task["title"])

        it, task = _find_row(page, t_future)
        assert f"截止:{d_future}" in it.text(), it.text()
        assert "逾期" not in it.text() and "今天到期" not in it.text()

        it, task = _find_row(page, t_bad)
        assert f"截止:{bad_due}" in it.text(), "坏日期应原样显示而不是崩"
        assert "逾期" not in it.text()
    finally:
        w.hide()


def test_due_label_helper_edges(qapp):
    from pages.today_page import TodayPage

    assert TodayPage._due_label({}) == ""
    assert TodayPage._due_label({"due": "not-a-date"}) == "截止:not-a-date"
    assert TodayPage._due_label(
        {"due": (date.today() - timedelta(days=2)).isoformat()}).startswith("逾期 2 天")
    assert TodayPage._due_label({"due": date.today().isoformat()}) == "今天到期"
