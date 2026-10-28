# -*- coding: utf-8 -*-
"""gui 层：每个顶层页在**可见的那一刻**必须画不透明页面底色（视觉 P0；R224 改懒刷契约）。

守的是一类"不崩、但内容看不见"的静默缺陷：页面若透明，窗口材质基座（Mica/Acrylic，
跟随**系统**主题）会透出来 → 「应用深色 + 系统浅色」= 白底 + 浅字。

历史：2026-10-10 首次修复，但当时是手写 16 页名单，后加的 4 页漏登记 → 深色下白底
（像素采样近白 79–82%，正常页 3–7%）；2026-10-15 改为唯一入口 + 遍历 actions.PAGES。

R224 换肤提速把「换肤瞬间 18 页全刷」改成「当前可见页立刻刷 + 其余标脏，显示前补齐」
（全量刷约 290ms = 17ms/页 × 18 页，而那一刻 17 页并不参与绘制）。**契约的判据随之从
"换肤后置"改成"显示前置"**：任何一页在被显示之前必须已是当前主题色。这不是放松防备——
R194 那类 P0 的触发条件是"用户看见了白底"，只要显示前必然补齐就不会发生；而且标脏只清
标记不清 QSS，未补刷的页面仍带上一版不透明底色，最坏也只是"旧色一帧"，绝不会透明。
本文件按新契约覆盖 4 件事：换肤后当前页立刻变 / 18 页逐页切过去都是当前色 /
启动后没有任何一页透明 / 一次换肤只让 1 页付 re-polish（防无声退回全量 eager）。
"""
import re
from pathlib import Path

from PySide6.QtCore import Qt

from helpers import boot_window, pump, settle

ROOT = Path(__file__).resolve().parent.parent.parent


def _pages(w):
    import actions
    return [(title, actions.page_of(w, attr)) for attr, title, *_ in actions.PAGES]


def test_visible_page_restyles_immediately_on_theme_change(qapp, hub_root):
    """换肤那一刻正在看的页面必须立刻变——否则会有"半帧旧色"的闪动。"""
    import tokens
    w = boot_window(qapp, hub_root)
    assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 30), \
        "扫描未完成"

    page = w.governance_page            # 曾漏登记的页，优先用它当靶子
    w.switchTo(page)
    tokens.apply_theme("light")
    settle(qapp, max_s=3.0)
    qss = page.styleSheet() or ""
    assert tokens.resolve("bg_page") in qss, f"当前页未随换肤立刻更新：{qss[:60]}"


def test_every_page_restyles_when_shown_after_theme_change(qapp, hub_root):
    """深浅两轮、18 页逐页切过去：每一页显示时都必须是当前主题的底色。"""
    import tokens
    w = boot_window(qapp, hub_root)
    assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 30), \
        "扫描未完成"

    bad = []
    for mode in ("light", "dark"):
        tokens.apply_theme(mode)
        settle(qapp, max_s=3.0)
        expect = tokens.resolve("bg_page")
        for title, page in _pages(w):
            assert page is not None, f"{title}:页面不存在"
            w.switchTo(page)                     # 显示前置：switchTo 内部补齐底色
            qss = page.styleSheet() or ""
            if "background" not in qss:
                bad.append(f"{mode}/{title}:无 background 声明")
            elif expect not in qss:
                bad.append(f"{mode}/{title}:{qss[:40]} != {expect}")
    assert not bad, f"这些页面显示时底色不是当前主题色（会白底不可见）：{bad}"


def test_no_page_left_transparent_after_boot(qapp, hub_root):
    """启动后（未换过肤）18 页都必须带不透明底色——防"从没上过色"的新页面。"""
    w = boot_window(qapp, hub_root)
    missing = []
    for title, page in _pages(w):
        if page is None:
            missing.append(f"{title}:页面不存在")
            continue
        if not page.testAttribute(Qt.WA_StyledBackground):
            missing.append(f"{title}:未设 WA_StyledBackground")
        elif "background" not in (page.styleSheet() or ""):
            missing.append(f"{title}:无 background 声明")
    assert not missing, f"页面未画不透明底色（深色主题下会白底不可见）：{missing}"


def test_theme_change_restyles_only_visible_page(qapp, hub_root, monkeypatch):
    """性能契约（R224）：一次换肤只对当前可见页付 re-polish。

    全量刷 18 页实测约 290ms，而那一刻 17 页不可见、刷了也看不见——换肤是高频交互，
    290ms 就是"点了没反应"。有人改回全量 eager 时这里立刻爆，不必靠手感发现。
    """
    from qfluentwidgets import Theme, qconfig

    import tokens
    import window as window_mod

    w = boot_window(qapp, hub_root)
    assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 30), \
        "扫描未完成"

    calls = []
    real = window_mod.apply_page_background

    def _spy(p):
        calls.append(p)
        return real(p)

    monkeypatch.setattr(window_mod, "apply_page_background", _spy)
    target = "light" if qconfig.theme == Theme.DARK else "dark"
    tokens.apply_theme(target)
    settle(qapp, max_s=3.0)

    assert len(calls) == 1, (
        f"一次换肤刷了 {len(calls)} 个页面（应只刷当前可见页；全量 eager 会多付约 290ms）")
    assert calls[0] is w.stackedWidget.currentWidget(), "刷的不是当前可见页"


def test_page_pixels_are_not_white_in_dark_theme(qapp, hub_root):
    """像素级判据（R194 就是靠像素采样定位的）：页面四角必须**等于**当前主题的 bg_page。

    样式表写着对不等于画出来对——材质基座、透明子控件、绘制顺序都可能让实际像素
    与 QSS 不一致。这里对每页 `render` 到**品红底**画布上再采样四角：
    透明页会露出品红、旧主题页会露出旧色，只有真画对了才等于期望值。
    这也是对 R224 懒刷的最终验收——换肤后**再打开**的页面，像素必须已是新主题色。
    """
    from PySide6.QtGui import QColor, QPixmap

    import tokens
    w = boot_window(qapp, hub_root)
    assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 30), \
        "扫描未完成"

    bad = []
    for mode in ("dark", "light"):
        tokens.apply_theme(mode)
        settle(qapp, max_s=3.0)
        hexval = tokens.resolve("bg_page").lstrip("#")
        want = QColor(int(hexval[0:2], 16), int(hexval[2:4], 16), int(hexval[4:6], 16))
        for title, page in _pages(w):
            w.switchTo(page)
            for _ in range(4):
                qapp.processEvents()
            pm = QPixmap(page.size())
            pm.fill(QColor("magenta"))       # 透明即露馅，不会"黑得刚好合格"
            page.render(pm)
            img = pm.toImage()
            for x, y in ((4, 4), (img.width() - 5, 4), (4, img.height() - 5)):
                c = img.pixelColor(x, y)
                off = max(abs(c.red() - want.red()), abs(c.green() - want.green()),
                          abs(c.blue() - want.blue()))
                if off > 6:
                    bad.append(f"{mode}/{title}:({x},{y})={c.name()} 期望 {want.name()}")
                    break
    assert not bad, f"页面实际像素与主题不符（透明/旧色/白底都会让文字不可见）：{bad}"


def test_page_background_only_applied_at_single_entry():
    """结构性守卫：上色只允许在 uikit.apply_page_background 一处实现。

    理由：2026-10-15 的复发根因就是"名单式"实现（window 里罗列页面元组 + 逐页
    setAttribute/setStyleSheet）。名单必然随新增页面漂移，所以禁掉任何第二处实现。
    """
    import uikit
    assert callable(getattr(uikit, "apply_page_background", None)), \
        "uikit.apply_page_background 不存在（页面底色唯一实现）"
    assert callable(getattr(uikit, "invalidate_page_background", None)), \
        "uikit.invalidate_page_background 不存在（懒刷标记唯一实现）"

    win_src = (ROOT / "window.py").read_text(encoding="utf-8")
    hits = [f"{i}:{ln.strip()[:60]}" for i, ln in enumerate(
        win_src.splitlines(), 1) if "WA_StyledBackground" in ln]
    assert not hits, (
        "window.py 不得自己设 WA_StyledBackground（应统一走 uikit.apply_page_background）："
        f"{hits}")

    assert "apply_page_background(page)" in win_src, "_add() 里必须调用统一入口"
    assert "invalidate_page_background(page)" in win_src, \
        "换肤时须对不可见页标脏（否则退回全量 re-polish）"
    assert "_apply_page_bgs" in win_src and "actions.PAGES" in win_src, \
        "主题变化时须遍历 actions.PAGES 重刷（不得回到手写名单）"

    # 懒刷的另一半在 switchTo：显示前补齐，缺了它就是"换肤后再点开的页面还是旧色"
    m = re.search(r"def switchTo\(self.*?\n(.*?)(?=\n    def )", win_src, re.S)
    assert m and "apply_page_background(page)" in m.group(1), \
        "switchTo 必须在显示前调用 apply_page_background（懒刷补齐点）"


def test_no_manual_page_list_in_apply_page_bgs():
    """_apply_page_bgs 内部不得再出现页面属性名罗列（回归根因）。"""
    src = (ROOT / "window.py").read_text(encoding="utf-8")
    m = re.search(r"def _apply_page_bgs\(self\):(.*?)(?=\n    def )", src, re.S)
    assert m, "未找到 _apply_page_bgs"
    body = m.group(1)
    bad = [n for n in ("overview_page", "project_page", "timeline_page", "hub_page",
                       "notes_page", "brain_page", "ledger_page", "stats_page",
                       "report_page", "search_page", "connect_page", "help_page",
                       "components_page", "settings_page", "today_page",
                       "signal_page", "governance_page", "playbook_page")
           if n in body]
    assert not bad, f"_apply_page_bgs 又出现手写页面名单（会随新页面漂移）：{bad}"
