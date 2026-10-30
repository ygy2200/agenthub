# -*- coding: utf-8 -*-
"""前沿队列分页（R803）：超过每组 20 条时要有总数说明与「显示更多」出口。"""
import brain


def _add(root, n, prefix="R803"):
    for i in range(n):
        err, _tid = brain.task_add(root, title=f"{prefix} 任务 {i:02d}", project="R803",
                                   priority=2, agent="gui")
        assert not err, err


def test_task_next_returns_more_than_20(hub_root):
    brain.init_db(hub_root)
    _add(hub_root, 25)

    nxt = brain.task_next(hub_root, limit=50)
    assert nxt["counts"]["ready"] == 25
    assert len(nxt["ready"]) == 25, "GUI 展开需要 >20 条的返回，旧上限会静默截断"

    capped = brain.task_next(hub_root, limit=500)
    assert len(capped["ready"]) == 25
