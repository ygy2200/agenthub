# -*- coding: utf-8 -*-
"""gui 层：任务治理可见性（R7，2026-10-21）。

守护：过期租约与逾期任务在桌面任务看板上必须**看得出来**，并且只标真正有问题的
任务——新鲜认领不能被误标（误报会让治理标识失去可信度）。
"""
from PySide6.QtWidgets import QLabel

from helpers import boot_window, pump


def _age_claim(root, tid):
    import brain
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at='2020-01-01T00:00:00', "
                     "updated='2020-01-01T00:00:00' WHERE id=?", (tid,))


def test_task_board_marks_stale_lease_and_overdue(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    _, stale_id = brain.task_add(hub_root, "悬空认领任务", project="甲-项目",
                                 due="2020-01-01")
    brain.task_claim(hub_root, stale_id, "zcode")
    _age_claim(hub_root, stale_id)
    _, fresh_id = brain.task_add(hub_root, "新鲜任务", project="甲-项目")
    brain.task_claim(hub_root, fresh_id, "dsh")

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.brain_page
    w.switchTo(page)
    page.reload()

    assert pump(qapp, lambda: any("悬空认领任务" in lb.text()
                                  for lb in page.findChildren(QLabel)), 10), "任务看板未加载"
    labels = page.findChildren(QLabel)
    texts = [lb.text() for lb in labels]
    assert any(t.startswith("逾期 ") for t in texts), texts
    stale_chips = [lb for lb in labels if lb.text() == "租约过期"]
    assert len(stale_chips) == 1, f"新鲜任务被误标或过期任务漏标：{texts}"
    assert "hub_claim" in stale_chips[0].toolTip(), stale_chips[0].toolTip()
    assert any("新鲜任务" in t for t in texts), texts


def test_task_board_shows_file_lease_chip(qapp, hub_root):
    """R14：挂在本任务上的文件租约在桌面可见（与 hub_tasks 的「锁 N」同口径），
    未挂锁的任务不得被误标；tooltip 给出路径/持有者/剩余时间。"""
    import brain
    import tokens

    brain.init_db(hub_root)
    _, tid = brain.task_add(hub_root, "改解析器", project="甲-项目")
    assert brain.task_claim(hub_root, tid, "zcode")[0] == ""
    err, _ = brain.file_lease_acquire(hub_root, "甲-项目/parser.py", "zcode",
                                      task_id=tid, ttl_min=30, reason="重构")
    assert err == "", err
    _, other = brain.task_add(hub_root, "不碰文件", project="甲-项目")
    assert brain.task_claim(hub_root, other, "dsh")[0] == ""

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.brain_page
    w.switchTo(page)
    page.reload()

    assert pump(qapp, lambda: any("改解析器" in lb.text()
                                  for lb in page.findChildren(QLabel)), 10), "任务看板未加载"
    labels = page.findChildren(QLabel)
    chips = [lb for lb in labels if lb.text() == "文件锁 1"]
    assert len(chips) == 1, [lb.text() for lb in labels]
    tip = chips[0].toolTip()
    assert "甲-项目/parser.py" in tip and "zcode" in tip and "剩" in tip, tip
