# -*- coding: utf-8 -*-
"""空库首启路径（R192）。

临时根 0 项目/0 记录实测：
  · 项目页「打开目录/复制路径/从 Inbox 引入」在无选中项目时全亮，点下去静默无反应；
  · 时间线 0 条记录却提示「放宽筛选」——把「首次空」误写成「筛选空」。
"""
from PySide6.QtWidgets import QLabel

from helpers import boot_window, settle


def _layout_texts(lay):
    out = []
    for i in range(lay.count()):
        it = lay.itemAt(i)
        w = it.widget()
        if w is not None:
            if isinstance(w, QLabel):
                out.append(w.text() or "")
            out.extend(lb.text() or "" for lb in w.findChildren(QLabel))
        elif it.layout() is not None:
            out.extend(_layout_texts(it.layout()))
    return out


def _visible_texts(page):
    # 只看当前仍在布局里的控件：重建时旧空态只是 deleteLater，findChildren 会把
    # 已移出布局、等待销毁的标签也算进来（R192 首跑误报）。
    return _layout_texts(page.box)


def test_project_actions_follow_selection(qapp, hub_root):
    import brain
    import core
    import tokens

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.project_page)
        settle(qapp, max_s=2.0)
        page = w.project_page
        assert page.listw.count() == 0
        # R196：库空时唯一引导搬到宽栏（记录区空态 + CTA），正文浏览器让位；
        # 左窄栏不再重复同一句话。
        empty = page._recEmpty
        assert empty is not None and empty.isVisible(), "库空时记录区没有引导"
        assert "还没有任何项目" in empty.titleLabel.text()
        assert not page.browser.isVisible(), "空态与正文同时出现"
        assert not page.listw._empty_hint_widget.isVisible(), "左栏与记录区重复引导"
        for b in (page.openBtn, page.copyBtn, page.inboxBtn):
            assert not b.isEnabled(), f"无选中项目时「{b.text()}」不应可点"

        err = core.create_project(hub_root, "R192空库-项目")
        assert not err, err
        # 项目页列表读大脑 projects 表（不是文件系统扫描），创建目录后需登记
        with brain.db_conn(hub_root) as conn:
            conn.execute("INSERT OR IGNORE INTO projects(name, created) VALUES(?,?)",
                         ("R192空库-项目", "2026-10-26"))
        page.set_snapshot(core.scan(hub_root))
        settle(qapp, max_s=2.0)
        assert page.listw.count() == 1
        assert page.listw.currentRow() == 0
        for b in (page.openBtn, page.copyBtn, page.inboxBtn):
            assert b.isEnabled(), f"选中项目后「{b.text()}」应可点"

        page.filterEdit.setText("绝对不存在R192")
        settle(qapp, max_s=1.0)
        assert page.listw.count() == 0
        # R196：无选中时不再用 24px「—」占位（视觉上像一条孤立短横线）
        assert not page.nameLabel.isVisible() and page.nameLabel.text() == ""
        assert "左侧选择项目查看工作记录" in page.browser.toPlainText()
        for b in (page.openBtn, page.copyBtn, page.inboxBtn):
            assert not b.isEnabled(), f"过滤到无项目后「{b.text()}」应重新收起"
    finally:
        w.hide()


def test_timeline_distinguishes_first_empty_from_filter_empty(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.timeline_page)
        settle(qapp, max_s=2.0)
        page = w.timeline_page
        texts = " ".join(_visible_texts(page))
        assert "还没有工作记录" in texts, texts
        assert "放宽筛选" not in texts, "空库不应提示用户去放宽不存在的筛选"

        brain.add_record(hub_root, project="R192有记录-项目", agent="zcode",
                         date="2026-10-26", title="空库测试记录", content="正文")
        with brain.db_conn(hub_root) as conn:
            conn.execute("INSERT OR IGNORE INTO projects(name, created) VALUES(?,?)",
                         ("R192空项目-项目", "2026-10-26"))
        page.set_records(brain.list_records(hub_root, limit=50))
        settle(qapp, max_s=2.0)
        texts = " ".join(_visible_texts(page))
        assert "还没有工作记录" not in texts
        assert "空库测试记录" in texts

        idx = page.projFilter.findText("R192空项目-项目")
        assert idx > 0, [page.projFilter.itemText(i) for i in range(page.projFilter.count())]
        page.projFilter.setCurrentIndex(idx)
        settle(qapp, max_s=2.0)
        texts = " ".join(_visible_texts(page))
        assert "没有符合条件的记录" in texts, texts
        assert "放宽筛选" in texts
        assert "还没有工作记录" not in texts

        page.projFilter.setCurrentIndex(0)
        settle(qapp, max_s=2.0)
        assert "空库测试记录" in " ".join(_visible_texts(page))
    finally:
        w.hide()
