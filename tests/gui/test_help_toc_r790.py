# -*- coding: utf-8 -*-
"""gui 层：帮助页目录在最小窗口内完整可见（R790）。

920×600 实机截图：目录「共 7 节」独占一行、行高 30px，第 7 节被挤出视口只剩
半截标题，用户以为帮助只有 6 节。计数并入标题行 + 行高 28 后应七节全露。
"""
from helpers import boot_window, settle


def test_toc_shows_all_sections_at_min_window(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.help_page)
        settle(qapp, max_s=1.5)
        page = w.help_page
        page.layout().activate()
        settle(qapp, max_s=0.5)

        assert page.tocCount.text() == f"共 {len(page.SECTIONS)} 节"
        assert page.tocList.count() == len(page.SECTIONS) == 7
        assert page.tocList.verticalScrollBar().maximum() == 0, \
            "最小窗下目录不应需要滚动才能看全"
        last = page.tocList.item(page.tocList.count() - 1)
        assert last.text().startswith("07")
        rect = page.tocList.visualItemRect(last)
        assert rect.bottom() <= page.tocList.viewport().height() - 1, \
            f"第 7 节被裁切：item bottom={rect.bottom()}，viewport={page.tocList.viewport().height()}"

        head = page.tocList.parentWidget().layout().itemAt(0).layout().itemAt(0).widget()
        assert abs(page.tocCount.y() - head.y()) <= 8, "节数应与「目录」标题同一行"
    finally:
        w.hide()


def test_toc_count_updates_when_filtered(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.help_page)
        settle(qapp, max_s=1.5)
        page = w.help_page
        page.tocFilter.setText("接入")
        settle(qapp, max_s=0.5)
        n = page.tocList.count()
        assert 0 < n < len(page.SECTIONS)
        assert page.tocCount.text() == f"命中 {n} / {len(page.SECTIONS)} 节"
        last = page.tocList.item(n - 1)
        assert page.tocList.visualItemRect(last).bottom() <= page.tocList.viewport().height() - 1

        page.tocFilter.setText("")
        settle(qapp, max_s=0.5)
        assert page.tocCount.text() == f"共 {len(page.SECTIONS)} 节"
    finally:
        w.hide()
