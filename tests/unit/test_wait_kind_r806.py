# -*- coding: utf-8 -*-
"""等待段成因区分（R806）。

真实库 #6/#8 是 status=blocked、deps 为空、blocked_by 写着「需要用户开代理 + 真机 TUN 复测」
/「用户决策」——工作台却把它们报成「等待依赖 2」，阻塞原因一个字都不露。
这里钉住：等前置任务与被外部挡住要分开计数，且每行带 `_wait_kind`。
"""
import brain


def _add(root, **kw):
    kw.setdefault("project", "R806")
    kw.setdefault("agent", "gui")
    err, tid = brain.task_add(root, **kw)
    assert not err, err
    return tid


def test_wait_kind_splits_deps_from_external_block(hub_root):
    brain.init_db(hub_root)
    front = _add(hub_root, title="R806 前置没做完")
    behind = _add(hub_root, title="R806 等它做完", deps=str(front))
    blocked = _add(hub_root, title="R806 等用户拍板", blocked_by="用户决策")

    nxt = brain.task_next(hub_root, owner="", limit=20)
    kinds = {t["id"]: t.get("_wait_kind") for t in nxt["waiting"]}
    assert kinds.get(behind) == "deps", "deps 里还有没做完的任务 → 等前置任务"
    assert kinds.get(blocked) == "blocked", "blocked_by 有原因、deps 为空 → 外部阻塞"
    assert front not in kinds, "前置任务本身没被挡住，应留在可认领段"

    c = nxt["counts"]
    assert c["waiting"] == 2 and c["waiting_deps"] == 1 and c["waiting_blocked"] == 1, c


def test_split_counts_survive_truncation(hub_root):
    """limit 只截列表，不截计数——摘要按总数说话，不能因为截断少算一条阻塞。"""
    brain.init_db(hub_root)
    for i in range(3):
        _add(hub_root, title=f"R806 阻塞 {i}", blocked_by=f"等环境 {i}")
    _add(hub_root, title="R806 依赖", deps="")
    front = _add(hub_root, title="R806 前置")
    _add(hub_root, title="R806 等前置", deps=str(front))

    small = brain.task_next(hub_root, owner="", limit=1)
    assert len(small["waiting"]) == 1, "limit 只该截列表"
    c = small["counts"]
    assert c["waiting"] == 4 and c["waiting_deps"] == 1 and c["waiting_blocked"] == 3, c
