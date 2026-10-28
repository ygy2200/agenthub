# -*- coding: utf-8 -*-
"""R223：主题切换提速后的两个不变量。

① lazy=True 只刷可见控件——**隐藏页在首次显示时必须被补刷**，否则用户切到那页会
   看到旧主题配色（qfw 用 dirty-qss + DirtyStyleSheetWatcher 在 Paint 时补刷，
   本用例用一个卡片控件的实际色值验证这条链真的通）。
② apply_accent 的进程内幂等：ACCENT_FILL 是常量，第二次调用必须直接返回，
   不能又跑一遍全量 updateStyleSheet（实测每次 360ms）。

注：不要拿 `getStyleSheet(styleSheetManager.source(x), theme)` 当期望值——本项目多数
控件注册的是 StyleSheetCompose，那条路子取出来是空串（实测），会得到"全是脏的"假红。
"""
from PySide6.QtWidgets import QWidget


def _first_card(page):
    from qfluentwidgets.components.widgets.card_widget import CardWidget
    cards = [x for x in page.findChildren(CardWidget) if x.styleSheet()]
    assert cards, "设置页没有已上样式的卡片，用例前提不成立"
    return cards[0]


def _dirty_count(page):
    n = 0
    for x in page.findChildren(QWidget):
        try:
            if x.property("dirty-qss"):
                n += 1
        except RuntimeError:
            continue
    return n


def test_hidden_page_is_restyled_when_first_shown(qapp, hub_root):
    import brain
    import tokens
    from helpers import boot_window, settle

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    dark_bg = tokens.C(tokens.BG_L1)
    w = boot_window(qapp, hub_root)
    try:
        w.switchTo(w.settings_page)
        settle(qapp, max_s=0.6)
        card = _first_card(w.settings_page)
        assert dark_bg in card.styleSheet(), f"深色下卡片底色不是 {dark_bg}"

        # 换主题时这页是隐藏的
        w.switchTo(w.overview_page)
        settle(qapp, max_s=0.4)
        tokens.apply_theme("light")
        light_bg = tokens.C(tokens.BG_L1)
        assert light_bg != dark_bg
        settle(qapp, max_s=0.5)

        w.switchTo(w.settings_page)
        settle(qapp, max_s=0.8)
        assert w.settings_page.isVisible()
        assert light_bg in card.styleSheet(), (
            f"隐藏页显示后卡片仍是旧主题底色（lazy 补刷没生效）：{card.styleSheet()[:70]}")
        # 已绘制的控件必须没有残留 dirty；滚动区下方那些从未绘制过的控件会继续挂着
        # dirty-qss 等首次 Paint（这正是懒刷的设计，不是遗漏）。
        vis_dirty = [x for x in w.settings_page.findChildren(QWidget)
                     if x.property("dirty-qss") and not x.visibleRegion().isNull()]
        assert vis_dirty == [], f"可见控件仍挂着 dirty-qss：{[type(x).__name__ for x in vis_dirty[:5]]}"
    finally:
        w.hide()


def test_apply_accent_second_call_is_noop(qapp, hub_root):
    import tokens

    tokens._ACCENT_APPLIED = None
    calls = []
    orig = tokens.setThemeColor            # tokens 直接绑定了名字，补丁必须打在它身上
    tokens.setThemeColor = lambda *a, **k: calls.append(1) or orig(*a, **k)
    try:
        tokens.apply_accent()
        tokens.apply_accent()
    finally:
        tokens.setThemeColor = orig
    assert len(calls) == 1, f"apply_accent 第二次仍在重复设置主色（调用 {len(calls)} 次）"
    assert tokens._ACCENT_APPLIED == tokens.ACCENT_FILL
