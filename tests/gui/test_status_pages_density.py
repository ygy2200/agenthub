# -*- coding: utf-8 -*-
"""状态/统计/随记三页首屏密度守卫（R182）。

920×600 最小窗实测：
  · 统计页把回溯性分布排在可行动摘要之前，体检与任务过程整体落在首屏之外；
  · 大脑页记忆行固定取 72 字，仍超出可用宽度，QLabel 硬切且没有省略号；
  · 随记页读取失败的异常分支引用了不存在的 noteList，错误态自身会崩；
  · 心情热力条没有任何图例，空库下用户不知道这排细线是什么。
"""
from PySide6.QtCore import QPoint

from helpers import boot_window, settle


def _at(page, widget):
    return widget.mapTo(page, QPoint(0, 0)).y()


def _first_agent_row(page):
    item = page.agentBox.itemAt(0)
    if item.widget():
        return item.widget()
    sub = item.layout()
    return sub.itemAt(0).widget() if sub and sub.count() else None


def test_stats_actionable_summary_fits_min_window(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.stats_page.set_db(brain.stats(hub_root))
        w.switchTo(w.stats_page)
        settle(qapp, max_s=1.5)
        page = w.stats_page
        assert page.healthLine.text(), "体检摘要应有内容"
        assert page.taskLine.text(), "任务过程摘要应有内容"
        assert _at(page, page.healthLine) < page.height() - 40, \
            "920×600 下大脑体检应落在首屏内"
        assert _at(page, page.taskLine) < page.height() - 40, \
            "920×600 下任务过程应落在首屏内"
        # 分布区仍完整保留（只是排在可行动摘要之后）
        first_row = _first_agent_row(page)
        assert first_row is not None, "分布区应仍有内容"
        assert _at(page, first_row) > _at(page, page.taskLine)
    finally:
        w.hide()


def test_brain_memory_rows_elide_instead_of_hard_clip(qapp, hub_root):
    import brain
    import tokens

    content = "长记忆预览" * 40
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, content, kind="lesson", agent="zcode")
    brain._bump_access(hub_root, [mid])  # top10 只收 use_count>0 的记忆
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.brain_page)
        w.brain_page.reload(force=True)
        settle(qapp, max_s=2.0)
        page = w.brain_page
        from uikit import ClickBodyLabel
        rows = [lb for lb in page.findChildren(ClickBodyLabel)
                if content[:20] in lb.toolTip()]
        assert rows, "未找到记忆预览行"
        lb = rows[0]
        assert lb.text().endswith("…"), f"长记忆行应右端省略，实际：{lb.text()[-12:]!r}"
        assert content in lb.toolTip(), "tooltip 应保留完整记忆正文"
    finally:
        w.hide()


def test_notes_heatmap_has_legend_and_count(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    brain.add_note(hub_root, "今天想通了一件事", source="human")
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.switchTo(w.notes_page)
        settle(qapp, max_s=1.5)
        hint = w.notes_page.heatHint
        assert "近 180 天" in hint.text()
        assert "有记 1 天" in hint.text(), f"热力条缺少可读统计：{hint.text()}"
    finally:
        w.hide()


def test_brain_top_ranking_collapses_to_five_and_expands(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    for i in range(8):
        mid = brain.add_memory(hub_root, f"第 {i} 条被唤起的长记忆内容" * 4,
                               kind="lesson", agent="zcode")
        brain._bump_access(hub_root, [mid])
    brain.log_search(hub_root, "search", "记忆", 8, agent="zcode")
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.brain_page)
        page = w.brain_page
        page.reload(force=True)
        settle(qapp, max_s=2.0)
        assert page.topBox.count() == 5, "默认应只显示前 5 条唤起排行"
        assert page.topToggle.isVisible()
        assert "展开全部 8 条" in page.topToggle.text()
        usage = page.useBox.itemAt(0)
        first_bar = usage.widget() or usage.layout().itemAt(0).widget()
        assert _at(page, first_bar) < page.height() - 20, \
            "收起到 5 条后，使用分布首行应落在 920×600 首屏内"
        page.topToggle.clicked.emit()
        settle(qapp, max_s=2.0)
        assert page.topBox.count() == 8, "展开后应显示全部 8 条"
        assert page.topToggle.text() == "收起"
        page.topToggle.clicked.emit()
        settle(qapp, max_s=1.0)
        assert page.topBox.count() == 5
    finally:
        w.hide()


def test_overview_has_no_horizontal_overflow_at_min_window(qapp, hub_root):
    """920×600：右栏不省略的 agent 行曾把内容最小宽度顶到 942px，整页横向溢出，
    最近动态标题被挤到 0 宽；修复后内容宽度应贴合视口且标题能读到省略号。"""
    import agentscore
    import brain
    import tokens
    from uikit import ClickBodyLabel

    brain.init_db(hub_root)
    title = "R183 很长的工作记录标题" * 6
    for _ in range(3):
        brain.add_record(hub_root, "AgentHub-多agent文件统一管理", "zcode",
                         "2026-10-26", title, "正文")
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.overview_page)
        page = w.overview_page
        page.set_db(brain.stats(hub_root), brain.list_records(hub_root, limit=20))
        page.set_agents(
            [agentscore.AgentInfo(name="ZCode", home="~/.zcode", detected=True)],
            {"zcode": {"online": True,
                       "last_project": "AgentHub-多agent文件统一管理" * 4,
                       "last_seen": "2026-10-26T10:00"}})
        settle(qapp, max_s=1.5)
        body = page.scroll.widget()
        body.layout().activate()
        settle(qapp, max_s=0.5)
        assert body.width() <= page.width() + 1, "总览内容宽于视口（横向溢出）"
        assert page.scroll.horizontalScrollBar().maximum() == 0
        titles = [lb for lb in page.findChildren(ClickBodyLabel)
                  if "R183 很长" in lb.toolTip() and lb.isVisible()]
        assert titles, "未找到最近动态标题行"
        assert max(t.width() for t in titles) > 150, \
            f"最近动态标题仍被挤窄：{[(t.width(), t.text()[:8]) for t in titles]}"
        assert titles[0].text().endswith("…")
    finally:
        w.hide()


def test_overview_cards_form_equal_grid(qapp, hub_root):
    """R186：FlowLayout 按 sizeHint 排成 3+1、右侧留空；等宽网格应在 920 下四卡同排，
    且窄宽度的行高翻倍（2 列），而不是把 4 列最小宽度强加给整页。"""
    import brain
    import tokens

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        page = w.overview_page
        w.switchTo(page)
        settle(qapp, max_s=1.5)
        page.scroll.widget().layout().activate()
        settle(qapp, max_s=0.5)
        cards = [page.cardProj, page.cardRec, page.cardToday, page.cardTodo]

        assert len({c.mapTo(page, QPoint(0, 0)).y() for c in cards}) == 1, \
            "920 宽应四卡同排"
        widths = [c.width() for c in cards]
        assert max(widths) - min(widths) <= 2, f"同排卡片应等宽：{widths}"
        lefts = sorted({c.mapTo(page, QPoint(0, 0)).x() for c in cards})
        assert len(lefts) == 4, f"应是 4 列而非换行：{lefts}"
        right = max(c.mapTo(page, QPoint(c.width(), 0)).x() for c in cards)
        assert right <= page.width() + 1, "卡片右缘超出视口"

        # 主窗最小 920，窄列路径直接量布局：内容 823 → 4 列一行；600 → 2 列两行
        grid = page.cardGrid
        row_h = cards[0].sizeHint().height()
        assert grid.heightForWidth(823) <= row_h + 4, "823 宽应一行"
        assert grid.heightForWidth(600) >= row_h * 2, "600 宽应折成两行"
        assert grid.minimumSize().width() <= 200, "网格不应把 4 列宽度强加给整页"
    finally:
        w.hide()


def test_brain_section_jump_reaches_offscreen_board(qapp, hub_root):
    """R187：920×600 下任务/待办/事件/置顶都在首屏之外；跳转条应显示各段条数，
    点击后把对应小标题带进视口（不再盲滚）。"""
    import brain
    import tokens

    brain.init_db(hub_root)
    brain.task_add(hub_root, "跳到任务看板", project="甲-项目", agent="zcode")
    brain.event_add(hub_root, "跳到事件流", project="甲-项目", agent="zcode")
    brain.add_memory(hub_root, "跳到置顶记忆", kind="lesson", agent="zcode", pinned=True)
    brain.add_record(hub_root, "甲-项目", "zcode", "2026-10-26", "一条带欠账的记录",
                     "后续：跳到待办看板的欠账内容")
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.brain_page)
        page = w.brain_page
        page.reload(force=True)
        settle(qapp, max_s=1.5)
        assert page._jump_links["task"].text() == "任务看板 1", \
            f"跳转条应带条数：{page._jump_links['task'].text()!r}"
        assert page._jump_links["event"].text() == "事件流 1"
        assert page._jump_links["pin"].text() == "置顶记忆 1"
        top_head = page._section_heads["top"]
        assert top_head.title_label.width() >= top_head.title_label.sizeHint().width() - 2, \
            ("区块标题被压窄："
             f"{top_head.title_label.width()} < {top_head.title_label.sizeHint().width()}")
        bar = page.scroll.verticalScrollBar()
        assert bar.maximum() > 0, "长页应可滚动"
        before = bar.value()
        page._jump_links["pin"].clicked.emit()
        settle(qapp, max_s=0.5)
        assert bar.value() > before, "点击置顶锚点后应向下滚动"
        head = page._section_heads["pin"]
        top = head.mapTo(page.scroll.viewport(), QPoint(0, 0)).y()
        assert -8 <= top <= page.scroll.viewport().height(), \
            f"置顶小标题应进入视口，实际 y={top}"
    finally:
        w.hide()


def test_notes_read_failure_renders_error_state(qapp, hub_root, monkeypatch):
    import brain
    import tokens
    from qfluentwidgets import StrongBodyLabel

    brain.init_db(hub_root)

    def boom(*a, **k):
        raise RuntimeError("database disk image is malformed")

    monkeypatch.setattr(brain, "list_notes", boom)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.switchTo(w.notes_page)
        w.notes_page.reload()
        settle(qapp, max_s=1.0)
        titles = [lb.text() for lb in w.notes_page.findChildren(StrongBodyLabel)]
        assert "随记读取失败" in titles, f"读取失败应显示页面级错误态：{titles}"
        assert w.notes_page.statLabel.text() == "—"
    finally:
        w.hide()
