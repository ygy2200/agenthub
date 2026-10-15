# -*- coding: utf-8 -*-
"""gui 层：每个顶层页都必须画**不透明页面底色**（视觉打磨 P0 / 2026-10-15）。

守的是一类"不崩、但内容看不见"的静默缺陷：
页面若透明，窗口材质基座（Mica/Acrylic，跟随**系统**主题）会透出来，于是
「应用深色 + 系统浅色」= 白底 + 浅色字、「应用浅色 + 系统深色」= 深底 + 深色字。

2026-10-10 修过一次，但当时是**手写 16 页名单**（其中 2 条还是别名），后加的
工作台 / 信号中心 / 治理中心 / 编排中心 4 页从未登记 → 深色主题下渲染成白底
（像素采样近白 79–82%，正常页仅 3–7%）。2026-10-15 改为统一入口（`window._add()`
→ `uikit.apply_page_background`），名单消失；本测试把"每页都有底色"钉死，
并额外守"上色只能在唯一入口里做"，防止再退回名单式写法。
"""
import re
from pathlib import Path

from PySide6.QtCore import Qt

from helpers import boot_window, pump, settle

ROOT = Path(__file__).resolve().parent.parent.parent


def _check_pages(w, mode, missing, wrong_color):
    import actions
    import tokens
    expect = tokens.resolve("bg_page")
    for attr, title, *_rest in actions.PAGES:
        page = actions.page_of(w, attr)
        if page is None:
            missing.append(f"{title}:页面不存在")
            continue
        if not page.testAttribute(Qt.WA_StyledBackground):
            missing.append(f"{title}:未设 WA_StyledBackground")
            continue
        qss = page.styleSheet() or ""
        if "background" not in qss:
            missing.append(f"{title}:无 background 声明")
        elif expect not in qss:
            wrong_color.append(f"{title}({mode}):{qss[:48]} != {expect}")


def test_all_pages_paint_opaque_background_both_themes(qapp, hub_root):
    """深浅两个主题下，18 个页面都必须刷上 tokens.bg_page。"""
    import tokens
    w = boot_window(qapp, hub_root)
    assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 30), \
        "扫描未完成"

    missing, wrong = [], []
    for mode in ("dark", "light"):
        tokens.apply_theme(mode)
        settle(qapp, max_s=3.0)     # 等 themeChangedFinished → _apply_page_bgs 重刷
        _check_pages(w, mode, missing, wrong)
    assert not missing, f"页面未画不透明底色（深色主题下会白底不可见）：{sorted(set(missing))}"
    assert not wrong, f"页面底色不等于 tokens.bg_page：{sorted(set(wrong))}"


def test_page_background_only_applied_at_single_entry():
    """结构性守卫：上色只允许在 uikit.apply_page_background 一处实现。

    理由：2026-10-15 的复发根因就是"名单式"实现（window 里罗列页面元组 + 逐页
    setAttribute/setStyleSheet）。名单必然随新增页面漂移，所以禁掉任何第二处实现。
    """
    import inspect

    import uikit
    assert callable(getattr(uikit, "apply_page_background", None)), \
        "uikit.apply_page_background 不存在（页面底色唯一实现）"

    win_src = (ROOT / "window.py").read_text(encoding="utf-8")
    hits = [f"{i}:{ln.strip()[:60]}" for i, ln in enumerate(
        win_src.splitlines(), 1) if "WA_StyledBackground" in ln]
    assert not hits, (
        "window.py 不得自己设 WA_StyledBackground（应统一走 uikit.apply_page_background）："
        f"{hits}")

    # 唯一入口：_add 里调用（新页面自动覆盖）+ _apply_page_bgs 遍历 actions.PAGES 重刷
    assert "apply_page_background(page)" in win_src, "_add() 里必须调用统一入口"
    assert "_apply_page_bgs" in win_src and "actions.PAGES" in win_src, \
        "主题变化时须遍历 actions.PAGES 重刷（不得回到手写名单）"


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
