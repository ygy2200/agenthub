# -*- coding: utf-8 -*-
"""统计页任务过程可扫读（R786）。

改前是一整段六维度长句（新建/完成/中位/P90/在办构成/重试/风险）+ 结尾「CLI：tasks --stats」。
改后：一句人话摘要 + 指标格（Grid-ish FlowLayout），命令行口径进 tooltip。
"""
from helpers import boot_window, pump, settle


def _bundle(**over):
    base = {
        "stats": {"projects": 2, "records": 3, "errors_open": 0, "projects_stalled": 0,
                  "searches_today": 0, "searches_total": 0},
        "health": {"dup_memory_count": 0, "todo_count": 0, "projects_stalled": 0,
                   "crystallization": 50, "searches_today": 0, "searches_total": 0},
        "distill": 0,
        "tasks": {
            "flow": {"created": 4, "done": 2, "cancelled_or_dead": 0,
                     "done_cycle_hours": {"p50": 3.5, "p90": 9}},
            "wip": {"in_progress": 2, "stale_leases": 1, "unowned": 0,
                    "approval_waiting": 1, "overdue": 0},
            "retry": {"tasks_with_attempts": 1, "attempts_total": 2},
            "risks": [{"kind": "lease_stale", "count": 1},
                      {"kind": "approval_waiting", "count": 1}],
        },
    }
    base.update(over)
    return base


def test_task_metrics_are_chips_not_run_on(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        page = w.stats_page
        w.switchTo(page)
        # 等首轮扫描 apply 落到 1、且它触发的统计取数也回来（空库＝0）。只看
        # _db_pending 会赶在 apply 之前返回，晚到的真实数据再把手喂的指标洗掉。
        # 不能等 _workers 清空：窗口构造时 init_db 线程从不摘除，永远非空。
        assert pump(qapp, lambda: w._apply_count >= 1 and not w._db_pending, 15), \
            "启动扫描/统计取数未落地"
        settle(qapp, 0.2)
        page.apply_db(_bundle())
        settle(qapp, 0.5)
        chips = page._task_chips
        assert chips["近 7 天新建"].valueLabel.text() == "4"
        assert chips["近 7 天完成"].valueLabel.text() == "2"
        assert chips["完成周期·中位"].valueLabel.text() == "3.5h"
        assert chips["完成周期·慢 10%"].valueLabel.text() == "9h"
        assert chips["在办"].valueLabel.text() == "2"
        assert chips["认领超时"].valueLabel.text() == "1"
        assert chips["等审批"].valueLabel.text() == "1"
        assert "认领超时 1" in chips["风险"].valueLabel.text()
        # 摘要行只说人话：不出现工具名/命令行/内部术语缩写
        s = page.taskLine.text()
        assert "CLI" not in s and "hub_" not in s, s
        assert "近 7 天新建 4 条、完成 2 条" in s
        assert "tasks --stats" in page.taskLine.toolTip(), "命令行口径应留在 tooltip"
    finally:
        w.hide()


def test_task_metrics_clear_when_unavailable(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        page = w.stats_page
        w.switchTo(page)
        assert pump(qapp, lambda: w._apply_count >= 1 and not w._db_pending, 15), \
            "启动扫描/统计取数未落地"
        settle(qapp, 0.2)
        page.apply_db(_bundle())
        settle(qapp, 0.3)
        assert page._task_chips
        page.apply_db(_bundle(tasks=None))
        settle(qapp, 0.3)
        assert page._task_chips == {}, "统计不可用时旧数字必须清掉"
        assert page.taskLine.text() == "任务统计暂不可用"
    finally:
        w.hide()
