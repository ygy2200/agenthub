# -*- coding: utf-8 -*-
"""运营报告/组件预览/设置三页首屏复核（R189）。

真实库 920×600 截图暴露三处：
  · 报告页先挂空态再填历史列表，5 份报告与「还没有运营报告」同时出现；
  · 组件页 13 枚徽章用 QHBoxLayout 排，超出内容宽后右缘被切并出现横向滚动条；
  · 设置页两条长说明没有换行，右侧文字被卡片边缘硬切。
"""
from pathlib import Path

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QLabel

from helpers import boot_window, settle

BADGE_TEXTS = {"ZCode", "WorkBuddy", "Claude Code", "hermes", "DSH", "Codex",
               "其他", "未知来源", "兴奋", "笃定", "疑问", "灵感", "平静"}


def test_report_empty_hint_hides_once_history_exists(qapp, hub_root):
    import tokens

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        page = w.report_page
        page.reload_reports()
        hint = getattr(page.histList, "_empty_hint_widget")
        assert page.histList.count() == 0
        assert hint.isVisibleTo(page), "空库时应显示空态引导"

        d = Path(hub_root) / "_hub" / "reports"
        d.mkdir(parents=True)
        (d / "反馈报告-2026-10-25.html").write_text("<html></html>", encoding="utf-8")
        page.reload_reports()
        assert page.histList.count() == 1
        assert not hint.isVisibleTo(page), "已有历史报告时空态必须收起"

        (d / "反馈报告-2026-10-25.html").unlink()
        page.reload_reports()
        assert hint.isVisibleTo(page), "报告删掉后空态要回来"
        texts = " ".join(lb.text() or "" for lb in hint.findChildren(QLabel))
        assert "生成并打开" in texts, "空态引导应与按钮实际文案一致"
    finally:
        w.hide()


def test_components_badges_wrap_without_horizontal_overflow(qapp, hub_root):
    import tokens

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.components_page)
        settle(qapp, max_s=2.5)
        page = w.components_page
        body = page.scroll.widget()
        assert page.scroll.horizontalScrollBar().maximum() == 0, "组件页不应出现横向滚动"
        from uikit import FlowLayout

        flow = next(f for f in body.findChildren(FlowLayout)
                    if any(f.itemAt(i).widget() is not None
                           and f.itemAt(i).widget().text() == "灵感"
                           for i in range(f.count())))
        chips = [flow.itemAt(i).widget() for i in range(flow.count())]
        assert len(chips) == 13, [c.text() for c in chips]
        # 注册表在不同数据根下可能回退成小写原名，按大小写无关比对
        assert {c.text().lower() for c in chips} == {t.lower() for t in BADGE_TEXTS}
        rows = {c.mapTo(body, QPoint(0, 0)).y() for c in chips}
        assert len(rows) >= 2, f"920 宽下徽章应换行，实际只有 {len(rows)} 行"
        for c in chips:
            p = c.mapTo(body, QPoint(0, 0))
            assert c.isVisibleTo(page), f"{c.text()} 不可见"
            assert p.x() + c.width() <= body.width(), \
                f"{c.text()} 右缘 {p.x() + c.width()} 超出内容宽 {body.width()}"
    finally:
        w.hide()


def test_settings_long_notes_wrap_inside_card(qapp, hub_root):
    import tokens

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.settings_page)
        settle(qapp, max_s=2.5)
        page = w.settings_page
        notes = [lb for lb in page.findChildren(QLabel) if "深色为默认" in (lb.text() or "")]
        assert notes, "设置页应保留外观说明"
        note = notes[0]
        assert note.wordWrap(), "长说明必须允许换行"
        if note.fontMetrics().horizontalAdvance(note.text()) > note.width():
            assert note.height() >= 2 * note.fontMetrics().height() - 2, \
                (note.height(), note.fontMetrics().height())
        parent = note.parentWidget()
        assert note.geometry().right() <= parent.contentsRect().right(), "说明文字应留在卡片内"
    finally:
        w.hide()
