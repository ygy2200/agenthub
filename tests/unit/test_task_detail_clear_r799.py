# -*- coding: utf-8 -*-
"""任务说明可清除（R799）。

task_update 的 detail 原来只在非空时写入——旧说明一旦写错就永远改不掉，
桌面「详情」里清空文本框也只会变成 no-op。这里钉住 "-" 清除语义与后续再写。
"""
import brain


def test_detail_dash_clears_and_can_be_rewritten(hub_root):
    brain.init_db(hub_root)
    err, tid = brain.task_add(hub_root, title="R799 详情清除", detail="初始说明",
                              agent="gui")
    assert not err, err

    err, row = brain.task_update(hub_root, tid, detail="-", agent="")
    assert not err, err
    assert row["detail"] == ""
    assert brain.task_get(hub_root, tid)["detail"] == ""

    err, row = brain.task_update(hub_root, tid, detail="补一句新说明", agent="")
    assert not err, err
    assert row["detail"] == "补一句新说明"


def test_detail_dash_does_not_touch_other_fields(hub_root):
    brain.init_db(hub_root)
    err, tid = brain.task_add(hub_root, title="R799 只清详情", detail="旧说明",
                              due="2026-11-09", agent="gui")
    assert not err, err
    err, row = brain.task_update(hub_root, tid, detail="-", agent="")
    assert not err, err
    assert row["detail"] == ""
    assert row["due"] == "2026-11-09"
    assert row["status"] == "pending"
