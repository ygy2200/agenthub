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
