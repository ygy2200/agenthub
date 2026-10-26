# -*- coding: utf-8 -*-
"""命令面板状态守卫（R174）：空输入不灌工具、项目直达、无结果恢复、键盘闭环。

2026-10-26 实测：Ctrl+K 空输入会倒出 18 页 + 7 动作 + 最多 60 项目 + 69 个 MCP
工具（150+ 行），入口被工具清单淹没；无结果只有一行「无匹配命令」，没有可试词。
"""
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from helpers import boot_window, insert_records, pump, settle


def _open_palette(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    insert_records(hub_root, 2, projects=2)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 20), \
        "项目页未完成首轮扫描"
    settle(qapp, max_s=1.0)
    pal = w._palette
    pal.popup()
    settle(qapp, max_s=1.0)
    assert pal.isVisible(), "命令面板未显示"
    return w, pal


def _labels(pal):
    return [pal.listw.item(i).text() for i in range(pal.listw.count())]


def test_palette_empty_state_shows_entry_actions_only(qapp, hub_root):
    import actions

    w, pal = _open_palette(qapp, hub_root)
    try:
        labels = _labels(pal)
        expected = len(actions.page_entries(w)) + len(actions.ACTIONS)
        assert len(labels) == expected, f"空输入应只列页面+动作，实际 {len(labels)} 行"
        assert all(lb.startswith(("打开：", "动作：", "外观：")) for lb in labels), \
            f"空输入混入了非入口项：{labels[:5]}"
        assert not any(lb.startswith(("工具：", "项目：")) for lb in labels), \
            "空输入不应把项目清单和 69 个 MCP 工具全部倒出来"
        assert pal.hint.text().startswith(f"{expected} 项")
        assert "可再输项目名/工具名" in pal.hint.text()
    finally:
        w.hide()


def test_palette_multiterm_and_project_direct(qapp, hub_root):
    w, pal = _open_palette(qapp, hub_root)
    try:
        pal.search.setText("项目 打开")
        settle(qapp, max_s=0.5)
        labels = _labels(pal)
        assert any(lb.startswith("项目：") for lb in labels), \
            f"多词 AND 未带出大脑里的项目行：{labels[:8]}"
        assert any("P0" in lb for lb in labels), "DB 项目应能直达（不依赖已建目录）"

        pal.search.setText("hub_search")
        settle(qapp, max_s=0.5)
        labels = _labels(pal)
        assert labels and all(lb.startswith("工具：") for lb in labels)
        assert any("hub_search" in lb for lb in labels)
    finally:
        w.hide()


def test_palette_no_result_and_keyboard_flow(qapp, hub_root):
    w, pal = _open_palette(qapp, hub_root)
    try:
        pal.search.setText("绝对不存在R174xyz")
        settle(qapp, max_s=0.5)
        assert pal.listw.count() == 2, "无结果应给「没匹配 + 可试词」两行"
        assert "无匹配命令" in pal.listw.item(0).text()
        assert not (pal.listw.item(0).flags() & Qt.ItemIsSelectable)
        assert "试试" in pal.listw.item(1).text()
        assert pal.hint.text().startswith("0 项")

        QTest.keyClick(pal.search, Qt.Key_Return)
        settle(qapp, max_s=0.5)
        assert pal.isVisible(), "无结果时回车不应关闭面板"

        pal.search.clear()
        settle(qapp, max_s=0.5)
        assert pal.listw.currentRow() == 0
        QTest.keyClick(pal.search, Qt.Key_Down)
        settle(qapp, max_s=0.3)
        assert pal.listw.currentRow() == 1, "↓ 应移动到下一项"
        QTest.keyClick(pal.search, Qt.Key_Up)
        settle(qapp, max_s=0.3)
        assert pal.listw.currentRow() == 0, "↑ 应回到上一项"
        QTest.keyClick(pal.search, Qt.Key_Escape)
        settle(qapp, max_s=0.3)
        assert not pal.isVisible(), "Esc 应关闭面板"
    finally:
        w.hide()
