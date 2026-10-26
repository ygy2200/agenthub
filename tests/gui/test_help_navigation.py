# -*- coding: utf-8 -*-
"""帮助页长文导航守卫（R172）：七节目录、锚点跳转、关键词过滤、窄窗可用。

2026-10-26 实测：帮助页只有一个 TextBrowser，七节正文（含 14 条页面说明与
5 条常见问题）从头滚到尾；用户找「怎么接入」「记录怎么撤」没有入口。
"""
import re

from helpers import boot_window, pump, settle


def _open_help(qapp, hub_root):
    import tokens

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    w.switchTo(w.help_page)
    assert pump(qapp, lambda: w.help_page.isVisible(), 5), "帮助页未显示"
    settle(qapp, max_s=1.0)
    return w, w.help_page


def _headings(page):
    return [re.sub(r"<[^>]+>", "", h).strip()
            for h in re.findall(r"<h2>(.*?)</h2>", page.CONTENT, re.S)]


def test_help_toc_covers_every_heading(qapp, hub_root):
    w, page = _open_help(qapp, hub_root)
    try:
        headings = _headings(page)
        assert len(headings) == 7
        assert page.tocList.count() == len(headings), "目录节数应与正文 h2 一致"
        texts = [page.tocList.item(i).text() for i in range(page.tocList.count())]
        for i, heading in enumerate(headings):
            assert heading in texts[i], f"第 {i + 1} 节目录项与正文标题不一致：{texts[i]}"
        assert page.tocCount.text() == f"共 {len(headings)} 节"
    finally:
        w.hide()


def test_help_toc_jumps_to_section(qapp, hub_root):
    w, page = _open_help(qapp, hub_root)
    try:
        sb = page.browser.verticalScrollBar()
        assert sb.maximum() > 0, "正文应长于视口，否则锚点跳转不可验证"

        page.tocList.setCurrentRow(page.tocList.count() - 1)
        settle(qapp, max_s=0.5)
        assert sb.value() > 0, "点最后一节目录后正文应滚下去"
        assert "命令行速查" in page.atLabel.text()

        page.tocList.setCurrentRow(0)
        settle(qapp, max_s=0.5)
        assert sb.value() <= 5, "回到第一节应回到正文顶部"
        assert "AgentHub 是什么" in page.atLabel.text()
    finally:
        w.hide()


def test_help_covers_new_pages_and_cli_quickref():
    """R185：帮助正文要与当前导航/CLI 能力同步（新增页、doctor --full、走查参数）。"""
    from pages.help_page import HelpPage

    text = HelpPage.CONTENT
    for word in ("信号中心", "编排中心", "设置", "doctor [--full]", "real_walkthrough",
                 "--size 920x600", "展开全部"):
        assert word in text, f"帮助正文缺少「{word}」"
    assert "默认桌面" not in text, "根目录默认值已过时（本机为 D:\\agent资料\\zcode项目记录）"


def test_help_cli_quickref_is_own_section(qapp, hub_root):
    """R190：CLI 速查原塞在常见问题末尾，现为独立小节——搜 doctor 直达、目录可跳。"""
    w, page = _open_help(qapp, hub_root)
    try:
        assert page.SECTIONS[-1]["title"] == "命令行速查（CLI）"
        assert 'name="help-sec-6"' in page.HTML

        page.tocFilter.setText("doctor")
        settle(qapp, max_s=0.8)
        assert page.tocList.count() == 1, "doctor 应只命中 CLI 速查一节"
        assert "命令行速查" in page.tocList.item(0).text()
        assert page.tocCount.text() == "命中 1 / 7 节"
        assert "命令行速查" in page.atLabel.text()

        page.tocFilter.clear()
        settle(qapp, max_s=0.8)
        page.tocList.setCurrentRow(6)
        settle(qapp, max_s=0.8)
        sb = page.browser.verticalScrollBar()
        top = page._heading_tops()[6]
        assert abs(sb.value() - min(top, sb.maximum())) <= 4, \
            (sb.value(), top, sb.maximum())
        assert "命令行速查" in page.atLabel.text()
    finally:
        w.hide()


def test_help_search_filters_and_highlights(qapp, hub_root):
    w, page = _open_help(qapp, hub_root)
    try:
        page.tocFilter.setText("快捷键")
        settle(qapp, max_s=0.5)
        assert page.tocList.count() == 1, "「快捷键」应只命中键盘快捷键一节"
        assert "键盘快捷键" in page.tocList.item(0).text()
        assert "键盘快捷键" in page.tocList.item(0).toolTip(), "目录 tooltip 应带节标题"
        assert page.tocCount.text() == "命中 1 / 7 节"
        assert page.browser.extraSelections(), "命中词应在正文中高亮"
        assert "键盘快捷键" in page.atLabel.text()

        page.tocFilter.setText("接入")
        settle(qapp, max_s=0.5)
        assert "接入公用大脑" in page.tocList.item(0).text(), \
            "标题命中的一节应排在正文顺带提及的页面说明之前"
        assert "接入公用大脑" in page.atLabel.text()

        page.tocFilter.setText("绝对不存在R172xyz")
        settle(qapp, max_s=0.5)
        assert page.tocList.count() == 2, "无结果时应给两行恢复提示"
        assert page.tocList.item(0).text() == "没有匹配的段落"
        assert page.tocCount.text() == "命中 0 / 7 节"
        assert "没有匹配" in page.atLabel.text()
        page.browser.verticalScrollBar().setValue(
            page.browser.verticalScrollBar().maximum())
        settle(qapp, max_s=0.5)
        assert "没有匹配" in page.atLabel.text(), "无结果提示不应被滚动联动覆盖"

        page.tocFilter.clear()
        settle(qapp, max_s=0.5)
        assert page.tocList.count() == 7, "清空检索后目录应恢复全部"
        assert page.tocCount.text() == "共 7 节"
        assert not page.browser.extraSelections(), "清空检索后应撤掉高亮"
    finally:
        w.hide()


def test_help_scroll_spy_and_back_to_top(qapp, hub_root):
    w, page = _open_help(qapp, hub_root)
    try:
        sb = page.browser.verticalScrollBar()
        assert not page.topBtn.isEnabled(), "正文在顶部时回顶按钮应禁用"

        sb.setValue(sb.maximum())
        settle(qapp, max_s=0.5)
        assert page.tocList.currentRow() == page.tocList.count() - 1, \
            "正文滚到底后目录应选中最后一节"
        assert "命令行速查" in page.atLabel.text()
        assert page.topBtn.isEnabled(), "离开顶部后回顶按钮应可用"

        page.topBtn.click()
        settle(qapp, max_s=0.5)
        assert sb.value() == 0
        assert page.tocList.currentRow() == 0, "回到顶部后目录应选中第一节"
        assert not page.topBtn.isEnabled(), "回到顶部后按钮应重新禁用"
    finally:
        w.hide()


def test_help_narrow_window_keeps_both_panes_usable(qapp, hub_root):
    w, page = _open_help(qapp, hub_root)
    try:
        w.resize(920, 600)
        assert pump(qapp, lambda: page.browser.width() < 800, 3), "窄窗下正文未重新布局"
        settle(qapp, max_s=1.0)
        assert page.tocFilter.isVisible() and page.tocList.isVisible()
        assert page.browser.isVisible()
        assert page.tocList.width() >= 150, "窄窗下目录不应被压到点不中"
        assert page.browser.width() >= 320, "窄窗下正文应保留可读宽度"
        assert page.browser.height() >= 180, "窄窗下正文应保留可读高度"
        last = page.tocList.item(page.tocList.count() - 1)
        rect = page.tocList.visualItemRect(last)
        assert rect.bottom() <= page.tocList.viewport().height() + 2, \
            "920×600 下第七节目录应完整可见（否则导航最后一节被裁）"
        assert not page.tocList.horizontalScrollBar().isVisible(), "目录不应出现横向滚动条"
    finally:
        w.hide()
