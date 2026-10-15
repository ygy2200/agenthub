# -*- coding: utf-8 -*-
"""gui 层：配色可达性守卫（UI 缺陷自查 Round 3 / 2026-10-13）。

守两件事，都是**曾经真出过、且不会再靠肉眼发现**的：
① 徽章色白字对比度 ≥4.5:1（WCAG AA 正文）——v2.24 声称"全部 ≥4.5:1"，
   实测兜底色 BADGE_FALLBACK #8a8a8a 只有 3.45:1（注释与实测矛盾，已修 #737373=4.74:1）。
   兜底色影响面最大：未登记 agent / 未知 kind / 未知心情 / 随记页来源标签都落到这里。
② pages/ 下不得出现硬编码色值——v2.24 已把 31 处色值收进 tokens，
   新增页面（signal/governance/playbook…）很容易重新内联，浅色主题下就是不可读。
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_badge_colors_pass_wcag_aa_with_white_text():
    """所有徽章底色配白字必须 ≥4.5:1（徽章是 11px 小字，属正文口径）。"""
    import tokens
    tables = {
        "AGENT_BADGE": tokens.AGENT_BADGE,
        "KIND_BADGE": tokens.KIND_BADGE,
        "MOOD_BADGE": tokens.MOOD_BADGE,
    }
    bad = []
    for tname, table in tables.items():
        for key, color in table.items():
            c = contrast(color, "#ffffff")
            if c < 4.5:
                bad.append(f"{tname}.{key}={color} {c:.2f}:1")
    c = contrast(tokens.BADGE_FALLBACK, "#ffffff")
    if c < 4.5:
        bad.append(f"BADGE_FALLBACK={tokens.BADGE_FALLBACK} {c:.2f}:1")
    assert not bad, f"徽章白字对比度不达 AA（需≥4.5:1）：{bad}"


def test_semantic_colors_pass_wcag_aa_on_both_themes():
    """语义色（success/warning/danger）在**浅底与深底**都要 ≥4.5:1。

    Round 3 实证：diff 曾用硬编码 #3c9a5f/#d9534f，深色底达标（5.15/4.57）
    但浅色底只有 3.34/3.77 ✗ ——只测一个主题就会漏。
    """
    import tokens
    bad = []
    for name in ("success", "warning", "danger"):
        for mode, bg in (("light", tokens.BG_L0["light"]), ("dark", tokens.BG_L0["dark"])):
            fg = tokens.resolve(name, mode)  # resolve 支持指定 mode（测试双主题）
            c = contrast(fg, bg)
            if c < 4.5:
                bad.append(f"{name}/{mode}={fg} on {bg} {c:.2f}:1")
    assert not bad, f"语义色对比度不达 AA：{bad}"


def test_full_cartesian_contrast_passes():
    """前景 × 底色 × 主题**全笛卡尔积**都要 ≥4.5:1（2026-10-15 P0-2）。

    原测试只验了"语义色 × BG_L0"三对，漏掉 BG_L2 —— 于是 TEXT_3(dark) 在 BG_L2
    上 4.09:1 不达标却一直逃逸（弹窗正文/代码块/hover 底都走 BG_L2）。
    现在改为脚本全量验算：python tools/contrast_audit.py
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import contrast_audit
    bad = contrast_audit.failures()
    assert not bad, f"对比度不达 AA（全笛卡尔积，需≥4.5:1）：{bad}"


def test_tokens_contrast_comments_match_script_output():
    """tokens.py 行尾的对比度注释必须与脚本实测一致（防手写注释与实测矛盾复发）。

    2026-10-13 抓过 BADGE_FALLBACK"注释写 ≥4.5、实测 3.45"；
    2026-10-15 又抓到 TEXT_3"注释写 5.0、实测 4.73"。两次同源：手写。
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import contrast_audit
    p = ROOT / "tokens.py"
    before = p.read_text(encoding="utf-8")
    try:
        contrast_audit._write_comments()
        after = p.read_text(encoding="utf-8")
    finally:
        p.write_text(before, encoding="utf-8")
    assert after == before, (
        "tokens.py 的对比度注释与实测不符，请跑：python tools/contrast_audit.py --write")


def test_pages_have_no_hardcoded_colors():
    """pages/ 下不得内联色值（色值单一来源 = tokens.py）。"""
    pat = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgb\(|rgba\(")
    hits = []
    for f in sorted((ROOT / "pages").glob("*.py")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
            # 去掉行内注释后仍有色值字面量才算（注释里写色号是文档，不算违规）
            body = line
            if "  #" in line:
                body = line.split("  #")[0]
            if pat.search(body):
                hits.append(f"{f.name}:{i}: {line.strip()[:80]}")
    assert not hits, f"pages/ 内联色值（应改用 tokens）：{hits}"
