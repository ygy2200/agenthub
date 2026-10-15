# -*- coding: utf-8 -*-
"""gui 层：间距/圆角**源码字面量**守卫（视觉打磨 P1 / 2026-10-15）。

守的是"规范写了但代码不接线"的老毛病：tokens.py 早就定义了 PAD_PAGE/PAD_CARD/
GAP_CARD/RADIUS_CTRL/RADIUS_CARD，实测**引用 0 次**——全仓散落 17 种边距写法、
10 种间距值（6 与 10 不在 4px 网格上）、6 种圆角（1/4/6/8/9/12px）。
改完值之后若没有守卫，半年内必然漂回去。

白名单（Fluent 2 阶梯的子集）：
  · 间距/边距：0 / 4 / 8 / 12 / 16 / 20 / 24 / 32
  · 圆角字面量：2（热力格等小色块 = Fluent borderRadiusSmall）/ 4 / 8
    —— 徽章与胶囊**不允许字面量**，必须走 uikit.chip_qss（圆角由高度推导）
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SPACING_GRID = {0, 4, 8, 12, 16, 20, 24, 32}
RADIUS_OK = {2, 4, 8}

_SCAN = ["uikit.py", "window.py"] + [f"pages/{p.name}" for p in sorted((ROOT / "pages").glob("*.py"))]
_RE_SPACING = re.compile(r"\b(?:setSpacing|addSpacing)\((\d+)\)")
_RE_MARGINS = re.compile(r"setContentsMargins\(([\d,\s]+)\)")
_RE_RADIUS = re.compile(r"border-radius:\s*(\d+)px")


def _code_lines(text: str):
    """去掉注释行与行尾注释，只扫真实代码。"""
    for i, line in enumerate(text.splitlines(), 1):
        body = line.split("  #")[0] if "  #" in line else line
        stripped = body.strip()
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("f'"):
            continue
        yield i, body, line


def test_spacing_values_are_on_grid():
    """setSpacing / addSpacing / setContentsMargins 的字面量必须在 7 档网格上。"""
    bad = []
    for rel in _SCAN:
        f = ROOT / rel
        if not f.exists():
            continue
        for i, body, _raw in _code_lines(f.read_text(encoding="utf-8")):
            for m in _RE_SPACING.finditer(body):
                if int(m.group(1)) not in SPACING_GRID:
                    bad.append(f"{rel}:{i} {m.group(0)}")
            for m in _RE_MARGINS.finditer(body):
                for v in m.group(1).split(","):
                    if int(v.strip()) not in SPACING_GRID:
                        bad.append(f"{rel}:{i} {m.group(0)}")
    assert not bad, f"间距不在 4px 网格（允许 {sorted(SPACING_GRID)}）：{bad}"


def test_radius_values_are_standard():
    """圆角字面量只允许 2/4/8；胶囊类必须走 chip_qss，不得手写。"""
    bad = []
    for rel in _SCAN:
        f = ROOT / rel
        if not f.exists():
            continue
        for i, body, _raw in _code_lines(f.read_text(encoding="utf-8")):
            for m in _RE_RADIUS.finditer(body):
                if int(m.group(1)) not in RADIUS_OK:
                    bad.append(f"{rel}:{i} {m.group(0)}")
    assert not bad, f"圆角非标准值（允许 {sorted(RADIUS_OK)}，胶囊请用 uikit.chip_qss）：{bad}"


def test_page_margins_use_single_entry():
    """页面外边距必须走 uikit.set_page_margins（值从 tokens.PAD_PAGE 派生）。

    理由：手写 `(24, 24, 24, 24)` 与 tokens.PAD_PAGE 只是巧合相同，改 token
    带不动页面——这正是上一轮"定义了却 0 引用"的成因。
    """
    bad = []
    for rel in _SCAN:
        f = ROOT / rel
        if not f.exists():
            continue
        for i, body, _raw in _code_lines(f.read_text(encoding="utf-8")):
            if re.search(r"setContentsMargins\(24, 24, 24, 24\)", body):
                bad.append(f"{rel}:{i}")
    assert not bad, f"页面外边距应改用 uikit.set_page_margins(lay)：{bad}"


def test_tokens_spacing_constants_are_actually_used():
    """反查：tokens 的间距/圆角常量必须**真被引用**（防再次定义-不接线）。"""
    src = "\n".join((ROOT / rel).read_text(encoding="utf-8") for rel in _SCAN
                    if (ROOT / rel).exists())
    unused = []
    for name in ("PAD_PAGE", "PAD_CARD", "GAP_CARD", "RADIUS_CTRL", "RADIUS_CARD",
                 "SPACE_S", "SPACE_M", "SPACE_L"):
        if not re.search(rf"tokens\.{name}\b", src):
            unused.append(name)
    assert not unused, f"tokens 定义了却无人引用（又变成摆设）：{unused}"


def test_gap_is_consumed():
    """tokens.gap() 必须真被页面代码引用（第三轮新增）。

    根因：gap() 定义完整（按密实度缩放 + 吸附回 4px 网格）却**零调用方**——
    密实度开关因此只调行高（row_height 有 3 个消费方）不调任何间距，
    用户切"紧凑"看不到间距变化，"间距缩放"是摆设。现由 FlowLayout 接线。
    """
    src = "\n".join((ROOT / rel).read_text(encoding="utf-8") for rel in _SCAN
                    if (ROOT / rel).exists())
    assert re.search(r"tokens\.gap\(", src), (
        "tokens.gap() 无人引用——密实度的间距缩放又是摆设（请在 FlowLayout/"
        "区块间距处接线）")


def test_section_icons_min(qapp, hub_root):
    """每页至少 2 个区块图标（第三轮新增）。

    依据：图标数是"扫读着陆点"的直接度量。审计实测信号中心/编排中心/Agent 中心/
    帮助/工作台/搜索/接入 ≤1 个，而有图标的页（总览 9、时间线 19）扫读明显更快。
    组件预览页本来就是样式陈列，按内容计不作要求。
    """
    import actions
    import brain
    import tokens
    from PySide6.QtWidgets import QWidget
    from helpers import boot_window, pump

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 40)
    low = []
    for attr, title, *_rest in actions.PAGES:
        if title == "组件预览":
            continue
        page = actions.page_of(w, attr)
        if page is None:
            continue
        w.switchTo(page)
        for _ in range(5):
            qapp.processEvents()
        n = len([c for c in page.findChildren(QWidget) if type(c).__name__ == "IconWidget"])
        if n < 2:
            low.append(f"{title}={n}")
    assert not low, f"这些页的区块图标少于 2 个（请用 section_head(text, icon)）：{low}"
    """setSpacing 必须走 tokens.SPACE_*（或 0），不允许裸网格整数。

    2026-10-15 第二轮扩面。上一轮只守"值在 4px 网格上"，于是 25 处 `setSpacing(8)`
    原样留着——**两套来源并存**：改 token 只改到一半，和第一轮"页头 28→24 只落地
    14/18"是同一种漂移。现在把"间距必须单一来源"也纳入守卫。
    边距（setContentsMargins）不在此列：那是每个容器自己的布局决策，走网格白名单即可。
    """
    bad = []
    for rel in _SCAN:
        f = ROOT / rel
        if not f.exists():
            continue
        for i, body, _raw in _code_lines(f.read_text(encoding="utf-8")):
            for m in re.finditer(r"setSpacing\(([^)]*)\)", body):
                arg = m.group(1).strip()
                if arg == "0":
                    continue
                if not arg.startswith("tokens.SPACE_"):
                    bad.append(f"{rel}:{i} {m.group(0)}")
    assert not bad, f"setSpacing 请改用 tokens.SPACE_*（间距单一来源）：{bad}"
