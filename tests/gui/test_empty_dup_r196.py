# -*- coding: utf-8 -*-
"""空态重复与空态文案过期（R196）。

两处真实缺陷（空库 920×600 截图 + 代码核对）：
  · 治理中心待审批空态：摘要行写「无待批任务——需复核的任务被认领后才会入队」，
    居中空态又写一遍「暂无待批任务 / 需复核的任务被认领后才进入队列」——一屏两遍；
  · 项目页：左窄栏空态 + 记录区提示说同一件事，且 `list_empty_guard` 建一次就
    复用，文案永远停在第一次创建的那句（空库 → 过滤无命中仍提示「还没有任何项目」）。
"""
from PySide6.QtWidgets import QListWidget

from helpers import boot_window, settle


def test_list_empty_guard_refreshes_text(qapp):
    """同一列表切换场景：文案必须跟着最后一次调用走。"""
    from uikit import list_empty_guard

    class Host(QListWidget):
        pass

    lw = Host()
    lay = lw.layout() or __import__("PySide6.QtWidgets", fromlist=["QVBoxLayout"]).QVBoxLayout(lw)
    hint = list_empty_guard(lw, lay, "还没有任何项目", "点新建项目")
    assert hint.titleLabel.text() == "还没有任何项目"
    hint2 = list_empty_guard(lw, lay, "没有匹配「abc」的项目", "换个关键词")
    assert hint2 is hint, "空态应复用同一个控件"
    assert hint.titleLabel.text() == "没有匹配「abc」的项目", "空态标题停在首次创建的那句"
    assert hint.descLabel.text() == "换个关键词"
    list_empty_guard(lw, lay, "无数据", "")
    assert hint.descLabel.text() == ""
    assert not hint.descLabel.isVisible(), "空描述应隐藏，而不是留一行空白"


def test_governance_empty_has_single_guidance(qapp, hub_root):
    """治理中心待审批为空时：只有空态在说话，摘要行收起。"""
    import brain
    import tokens

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    try:
        w.resize(920, 600)
        w.switchTo(w.governance_page)
        settle(qapp, max_s=2.0)
        gp = w.governance_page
        sub = gp.subs[0]
        assert sub is not None, "治理中心分段结构变了，测试需要同步"
        assert sub.listw.count() == 0
        assert not sub.sumLabel.isVisible(), f"空态与摘要行重复：{sub.sumLabel.text()}"
        hint = sub.listw._empty_hint_widget
        assert hint.isVisible() and hint.titleLabel.text()
        # 唯一引导文案里必须给出「为什么空」的解释
        assert hint.descLabel is not None and hint.descLabel.text()
    finally:
        w.hide()
