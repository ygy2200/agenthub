# -*- coding: utf-8 -*-
"""工作台「等待」段说人话（R806）。

截图上页头写「等待依赖 2」，可 #6/#8 的 deps 是空的——它们是外部阻塞（等人拍板/等环境），
库里 blocked_by 早就写着原因，界面却一个字都不显示。这里钉住摘要口径、行内原因与 tooltip。
"""
from helpers import boot_window, settle


def _add(root, **kw):
    import brain
    kw.setdefault("project", "R806")
    kw.setdefault("agent", "gui")
    err, tid = brain.task_add(root, **kw)
    assert not err, err
    return tid


def _row(page, tid):
    for i in range(page.listw.count()):
        t = page._item2task.get(id(page.listw.item(i)))
        if t and int(t["id"]) == int(tid):
            return page.listw.item(i)
    return None


def _heads(page):
    return [page.listw.item(i).text() for i in range(page.listw.count())
            if page.listw.item(i).text().startswith("──")]


def test_blocked_row_states_the_reason(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    blocked = _add(hub_root, title="R806 等用户拍板", blocked_by="用户决策")
    front = _add(hub_root, title="R806 前置没做完")
    _add(hub_root, title="R806 等它做完", deps=str(front))

    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)

        summary = page.sumLabel.text()
        assert "外部阻塞 1" in summary, f"外部阻塞被算成了等待依赖：{summary}"
        assert "等待依赖 1" in summary, f"真等前置任务的那条也要算：{summary}"

        row = _row(page, blocked)
        assert row is not None, "阻塞任务没进列表"
        assert "等:用户决策" in row.text(), row.text()
        tip = row.toolTip()
        assert "阻塞原因：用户决策" in tip, tip
        assert "下一步：" in tip, tip

        heads = _heads(page)
        assert any("等待依赖 / 外部阻塞" in h for h in heads), heads
    finally:
        w.hide()


def test_pure_external_block_renames_the_group(qapp, hub_root):
    """整段都是外部阻塞时，段标题不该还写「等待依赖」。"""
    import brain

    brain.init_db(hub_root)
    _add(hub_root, title="R806 等环境", blocked_by="需要真机复测")
    _add(hub_root, title="R806 可做的活")

    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)

        assert "外部阻塞 1" in page.sumLabel.text(), page.sumLabel.text()
        assert "等待依赖" not in page.sumLabel.text(), "没人等前置任务时不该出现等待依赖"
        heads = _heads(page)
        assert any(h.startswith("── 外部阻塞（1）") for h in heads), heads
    finally:
        w.hide()
