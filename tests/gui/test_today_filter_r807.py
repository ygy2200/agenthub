# -*- coding: utf-8 -*-
"""工作台即时筛选（R807）。

任务一多，六段列表只能肉眼扫；顶部筛选框要能按标题/项目/#编号/负责人/阻塞原因
即时收窄，并把「命中几条」说在摘要和分组标题上，清空即还原。
"""
import brain

from helpers import boot_window, settle


def _add(root, **kw):
    kw.setdefault("project", "R807")
    kw.setdefault("agent", "gui")
    err, tid = brain.task_add(root, **kw)
    assert not err, err
    return tid


def _texts(page):
    return [page.listw.item(i).text() for i in range(page.listw.count())]


def test_filter_narrows_by_words_project_and_id(qapp, hub_root):
    brain.init_db(hub_root)
    a = _add(hub_root, title="Alpha 代理排查", project="NetProj")
    _add(hub_root, title="Beta 存档备份", project="GameProj")
    _add(hub_root, title="Gamma 报告", project="NetProj")

    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)
        assert len(page._item2task) == 3, "起始应有 3 条任务"

        page.filterEdit.setText("alpha")
        settle(qapp, 0.5)
        rows = _texts(page)
        assert any("Alpha 代理排查" in s for s in rows), rows
        assert not any("Beta 存档备份" in s for s in rows), rows
        assert any("匹配 1 · 共" in s for s in rows), rows
        assert "筛选命中 1" in page.sumLabel.text(), page.sumLabel.text()

        # 多词是「都要命中」；项目名也参与匹配
        page.filterEdit.setText("netproj gamma")
        settle(qapp, 0.5)
        rows = _texts(page)
        assert any("Gamma 报告" in s for s in rows), rows
        assert not any("Alpha 代理排查" in s for s in rows), rows

        # #编号能直达
        page.filterEdit.setText(f"#{a}")
        settle(qapp, 0.5)
        assert [s for s in _texts(page) if "Alpha" in s], _texts(page)

        # 无命中给出口，不说「队列为空」
        page.filterEdit.setText("没有这个词")
        settle(qapp, 0.5)
        rows = _texts(page)
        assert any("没有匹配" in s for s in rows), rows
        assert not any("前沿队列为空" in s for s in rows), rows
        assert "筛选命中 0" in page.sumLabel.text(), page.sumLabel.text()

        # 清空即还原，摘要不再挂筛选项
        page.filterEdit.clear()
        settle(qapp, 0.5)
        assert len(page._item2task) == 3, "清空筛选应还原全部任务"
        assert "筛选命中" not in page.sumLabel.text(), page.sumLabel.text()
    finally:
        w.hide()


def test_blocked_reason_is_searchable(qapp, hub_root):
    """外部阻塞原因（R806 行内可见）也应能搜到——用户找「等谁」时第一反应是搜原因。"""
    brain.init_db(hub_root)
    _add(hub_root, title="等网络复测", blocked_by="需要真机 TUN 复测")
    _add(hub_root, title="普通待办")

    w = boot_window(qapp, hub_root)
    try:
        page = w.today_page
        w.switchTo(page)
        page.reload()
        settle(qapp, 0.8)

        page.filterEdit.setText("tun")
        settle(qapp, 0.5)
        rows = _texts(page)
        assert any("等网络复测" in s for s in rows), rows
        assert not any("普通待办" in s for s in rows), rows
    finally:
        w.hide()
