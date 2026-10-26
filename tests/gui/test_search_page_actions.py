# -*- coding: utf-8 -*-
"""搜索页交互态守卫（R166）：操作按钮必须跟随选中项，空态要能一键起步。

2026-10-26 实测三个问题：
  · 首次进入/清空结果后，右侧「打开/打开所在位置/跳到项目/复制内容」仍全部可见，
    点击却静默无反应（未选中任何结果）；
  · 搜索命中后不自动选中，用户还得再点一次才有预览；
  · 无结果/分类空只给一行生硬提示，预览区仍是「点选左侧条目查看详情」。
"""
from PySide6.QtWidgets import QPushButton

from helpers import boot_window, pump, settle

ACTIONS = (("打开", "btnOpen"), ("打开所在位置", "btnLoc"),
           ("跳到项目", "btnJump"), ("复制内容", "btnCopy"))


def _visible_actions(sp):
    return [label for label, attr in ACTIONS if getattr(sp, attr).isVisible()]


def _run_search(w, qapp, kw):
    sp = w.search_page
    sp.edit.setText(kw)
    sp.run()
    settle(qapp, max_s=1.0)


def test_search_page_action_states(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    brain.add_record(hub_root, "R166搜索验证项目", "zcode", "2026-10-26",
                     "字形守卫与搜索操作态", "含关键词 独角兽验证词 的记录正文")
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    sp = w.search_page
    try:
        w.switchTo(sp)
        assert pump(qapp, lambda: sp.isVisible(), 5), "搜索页未显示"
        settle(qapp, max_s=1.0)

        hint = getattr(sp.result, "_empty_hint_widget", None)
        assert hint is not None and hint.isVisible(), "首次进入应有空态引导"
        assert not sp.split.isVisible(), "未检索时不应摆出空列表+空预览右栏"
        assert sp.result.currentRow() == -1
        assert _visible_actions(sp) == [], "未选中结果时不应出现操作按钮"

        # 空态 CTA：一键把最近一条记录的项目名带进搜索框
        cta = [b for b in hint.findChildren(QPushButton) if b.text() == "搜索最近项目"]
        assert cta, "空态缺少「搜索最近项目」入口"
        cta[0].click()
        settle(qapp, max_s=1.5)
        assert sp.edit.text() == "R166搜索验证项目"
        assert sp.result.count() > 0, "一键搜索应带出结果"
        assert sp.split.isVisible(), "出结果后应展示列表+预览"
        row = sp.result.currentRow()
        assert row >= 0 and sp.view_hits[row][0] == "record", \
            "命中后应自动选中首条真实结果（跳过全盘提示行）并预览"
        assert "字形守卫与搜索操作态" in sp.preview.toPlainText()
        assert {"跳到项目", "复制内容"} <= set(_visible_actions(sp))
        assert "打开" not in _visible_actions(sp)
        assert "打开所在位置" not in _visible_actions(sp)

        # 切到无结果的分类：提示行可选中，按钮不悬空
        sp.kindFilter.setCurrentIndex(4)  # 任务
        settle(qapp, max_s=0.5)
        assert _visible_actions(sp) == [], "分类空时不应残留操作按钮"
        assert "该分类下无结果" in sp.preview.toPlainText(), "分类空应给出恢复路径"

        # 清空关键词：旧结果/操作全部收起，回到空态
        sp.kindFilter.setCurrentIndex(0)
        sp.edit.clear()
        settle(qapp, max_s=0.5)
        assert sp.result.count() == 0, "清空搜索词后应清掉旧结果"
        assert _visible_actions(sp) == []
        assert hint.isVisible(), "清空后应重新显示空态引导"
        assert not sp.split.isVisible(), "清空后应收起列表+预览，回到空态"

        # 无结果：预览给出下一步，而不是「点选左侧条目」
        _run_search(w, qapp, "绝对不存在的关键词R166xyz")
        assert _visible_actions(sp) == []
        body = sp.preview.toPlainText()
        assert "未找到" in body and "更短" in body, f"无结果缺少可执行建议：{body[:120]}"
    finally:
        # 不能 close()：主窗口 closeEvent 会排一个 2s 硬退出定时器，
        # 同一 pytest 进程里的后续用例会被 os._exit(0) 截断（R166 实测）。
        w.hide()


def test_search_keyboard_flow_reaches_results(qapp, hub_root):
    """R188：搜索页应能全程键盘完成——输入框 ↓ 进列表、Enter 打开、Esc 回输入框。"""
    import brain
    import tokens
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    brain.init_db(hub_root)
    for i in range(2):
        brain.add_record(hub_root, "R188键盘项目", "zcode", "2026-10-26",
                         f"键盘可达记录{i}", "含 键盘可达词 的正文")
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    sp = w.search_page
    try:
        w.switchTo(sp)
        settle(qapp, max_s=1.0)
        sp.edit.setText("键盘可达词")
        sp.run()
        settle(qapp, max_s=1.0)
        assert len(sp.view_hits) >= 2, "应至少命中两条记录"
        assert sp.result.currentRow() >= 0, "检索完成后应自动选中结果"

        QTest.keyClick(sp.edit, Qt.Key_Down)
        settle(qapp, max_s=0.3)
        assert sp.result.hasFocus(), "↓ 应把焦点交给结果列表"
        assert sp.result.currentRow() == 1, "↓ 应在首条基础上移到第二条"

        QTest.keyClick(sp.result, Qt.Key_Up)
        assert sp.result.currentRow() == 0, "列表内 ↑ 应回到第一条"

        calls = []
        sp.open_current = lambda: calls.append(1)
        QTest.keyClick(sp.result, Qt.Key_Return)
        settle(qapp, max_s=0.3)
        assert calls, "列表 Enter 应打开当前结果"

        QTest.keyClick(sp.result, Qt.Key_Escape)
        assert sp.edit.hasFocus(), "Esc 应回到搜索框"
        assert sp.edit.selectedText() == sp.edit.text(), "回到搜索框应全选便于改写"
    finally:
        w.hide()
