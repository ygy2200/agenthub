# -*- coding: utf-8 -*-
"""gui 层：工作台六段队列与任务行完整信息（R176）。

守两件事：
  1) 退避中/已延后不再从 GUI 消失——摘要有量、列表有分组；
  2) 标题不再硬截 60 字，行内放不下的失败原因/依赖/到点时间在 tooltip 里给全。
"""
from PySide6.QtWidgets import QWidget

from helpers import track_window


def _task(i, group, title="任务"):
    t = {"id": i, "project": "演示项目", "title": title, "owner": "",
         "status": "pending", "_priority": 1, "due": "", "last_error": "",
         "next_retry_at": "", "defer_until": "", "attempts": 0}
    if group == "doing":
        t.update(status="in_progress", owner="zcode")
    elif group == "takeover":
        t.update(status="in_progress", owner="slowpoke", _takeover="租约过期")
    elif group == "waiting":
        # brain._task_deps_waiting 现在返回任务行 dict；同时兼容旧的纯 id 列表。
        t.update(status="blocked", _wait=[{"id": 90, "title": "前置"}, 91])
    elif group == "backoff":
        t.update(_retry_wait_min=12, attempts=2, last_error="网络超时，稍后重试",
                 next_retry_at="2026-10-26T12:00:00")
    elif group == "deferred":
        t.update(_defer_wait_h=3.5, defer_until="2026-10-29")
    return t


def _page(qapp, hub_root, monkeypatch):
    import brain
    from pages.today_page import TodayPage

    long_title = "跨多个 agent 的完整任务标题" * 8
    groups = {k: [_task(i, k, long_title if k == "backoff" else f"{k} 任务")]
              for i, k in enumerate(
                  ("ready", "doing", "takeover", "waiting", "backoff", "deferred"), 1)}
    counts = {k: len(v) for k, v in groups.items()}
    nxt = dict(groups)
    nxt.update(counts=counts, control={"agent": "", "state": "active", "note": ""})

    monkeypatch.setattr(brain, "task_next", lambda *a, **k: nxt)
    monkeypatch.setattr(brain, "team_digest", lambda *a, **k: {
        "flow": {"records": 3, "tasks_done": 1},
        "current": {"wip": {"stale_leases": 0, "approval_waiting": 0}, "open_total": 6},
        "risks": [], "suggestions": []})

    win = QWidget()
    win.root = str(hub_root)
    track_window(win)          # B019：不登记的裸窗口不销毁，qfw 注册表会把整页留住
    page = TodayPage(win, win)
    page.reload()
    return win, page, long_title


def _texts(page):
    return [page.listw.item(i).text() for i in range(page.listw.count())]


def test_today_shows_backoff_and_deferred_groups(qapp, hub_root, monkeypatch):
    win, page, _ = _page(qapp, hub_root, monkeypatch)
    try:
        summary = page.sumLabel.text()
        assert "退避中 1" in summary and "已延后 1" in summary
        texts = _texts(page)
        assert any("退避中（1）" in t for t in texts)
        assert any("已延后（1）" in t for t in texts)
        backoff = next(t for t in texts if "退避中" not in t and "12 分钟后可再试" in t)
        assert "第 2 次失败" in backoff
        deferred = next(t for t in texts if "3.5 小时后到点" in t)
        assert "2026-10-29" not in deferred, "行内只留相对时间，精确时间放 tooltip"
    finally:
        win.close()


def test_today_rows_keep_full_title_and_details_in_tooltip(qapp, hub_root, monkeypatch):
    win, page, long_title = _page(qapp, hub_root, monkeypatch)
    try:
        items = [page.listw.item(i) for i in range(page.listw.count())]
        backoff = next(it for it in items if "12 分钟后可再试" in it.text())
        assert long_title in backoff.text(), "行数据不应再硬截标题"
        tip = backoff.toolTip()
        assert long_title in tip
        assert "上次失败：网络超时，稍后重试" in tip
        assert "退避剩余：12 分钟（到 2026-10-26T12:00:00）" in tip

        takeover = next(it for it in items if "可接管（1）" not in it.text()
                        and "原因:租约过期" in it.text())
        assert "可接管原因：租约过期" in takeover.toolTip()

        waiting = next(it for it in items if "等待:#90,#91" in it.text())
        assert "等待依赖：#90、#91" in waiting.toolTip()
    finally:
        win.close()
