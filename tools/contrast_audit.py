# -*- coding: utf-8 -*-
"""配色对比度全量验算（WCAG 2.1）——tokens 注释里的数字**必须由本脚本生成**。

为什么要有这个脚本（2026-10-15 P0-2）：
    tokens.py 原来手写的对比度注释与实测不符（写着 TEXT_3 "5.0:1 / 4.5:1"，
    实测 4.73 / 5.29），而且只算了文字 vs BG_L0 一组——**漏掉 BG_L2**，
    于是 TEXT_3(dark) 在 BG_L2 上 4.09:1 不达标却一直没人发现
    （弹窗正文、代码块、hover 底都走 BG_L2）。

用法：
    python tools/contrast_audit.py            # 打印全表，不达标项列在最前
    python tools/contrast_audit.py --write    # 把实测数字回写进 tokens.py 的行尾注释
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tokens  # noqa: E402

AA_NORMAL = 4.5   # 正文
AA_LARGE = 3.0    # 大字（≥18px 或 14px 粗体）


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


def backgrounds() -> dict:
    return {"BG_L0": tokens.BG_L0, "BG_L1": tokens.BG_L1, "BG_L2": tokens.BG_L2}


def foregrounds() -> dict:
    names = ("TEXT_1", "TEXT_2", "TEXT_3", "ACCENT", "SUCCESS", "WARNING", "DANGER")
    return {n: getattr(tokens, n) for n in names}


def text_pairs() -> list:
    """前景（文字/语义色） × 底色（三种层级） × 主题 —— 全笛卡尔积。"""
    out = []
    for fname, fg in foregrounds().items():
        for bname, bg in backgrounds().items():
            for mode in ("light", "dark"):
                out.append((fname, bname, mode, contrast(fg[mode], bg[mode])))
    return out


def badge_pairs() -> list:
    """徽章色（白字底）全量。"""
    out = []
    for tname in ("AGENT_BADGE", "KIND_BADGE", "MOOD_BADGE"):
        for key, color in getattr(tokens, tname).items():
            out.append((f"{tname}.{key}", color, contrast("#ffffff", color)))
    out.append(("BADGE_FALLBACK", tokens.BADGE_FALLBACK,
                contrast("#ffffff", tokens.BADGE_FALLBACK)))
    return out


def failures() -> list:
    bad = [f"{f}@{b}/{m} {v:.2f}:1" for f, b, m, v in text_pairs() if v < AA_NORMAL]
    bad += [f"{n}={c} 白字 {v:.2f}:1" for n, c, v in badge_pairs() if v < AA_NORMAL]
    return bad


def main() -> int:
    write = "--write" in sys.argv
    print("=== 文字/语义色 × 三档底色 × 双主题（AA 正文 4.5:1）===")
    for fname, _spec in foregrounds().items():
        row = []
        for mode in ("light", "dark"):
            for bname, bg in backgrounds().items():
                fg = getattr(tokens, fname)
                row.append(f"{bname}/{mode[0]}={contrast(fg[mode], bg[mode]):5.2f}")
        print(f"  {fname:9}" + "  ".join(row))

    print("\n=== 徽章（白字，≥4.5:1）===")
    worst = sorted(badge_pairs(), key=lambda x: x[2])[:5]
    for n, c, v in badge_pairs():
        if v < AA_NORMAL + 0.2:
            print(f"  贴线/不达标 {n} {c} {v:.2f}:1")
    print(f"  最低 5 个： " + "，".join(f"{n} {v:.2f}" for n, _c, v in worst))

    bad = failures()
    print(f"\n=== 不达标项（{len(bad)}）===")
    for x in bad:
        print("  ✗", x)
    if not bad:
        print("  （无）")

    if write:
        _write_comments()
        print("\n已把实测数字回写进 tokens.py 行尾注释")
    return 1 if bad else 0


def _write_comments() -> None:
    """把 TEXT_1/2/3 行尾注释替换成脚本实测值（防"手写注释与实测不符"复发）。"""
    p = ROOT / "tokens.py"
    src = p.read_text(encoding="utf-8")
    for name in ("TEXT_1", "TEXT_2", "TEXT_3"):
        spec = getattr(tokens, name)
        vals = [contrast(spec[m], bg[m]) for m in ("light", "dark")
                for bg in (tokens.BG_L0, tokens.BG_L1, tokens.BG_L2)]
        light_min, dark_min = min(vals[:3]), min(vals[3:])
        pat = re.compile(rf"(?m)^({name} = \{{[^}}]*\}})([ \t]+#.*)?$")
        src = pat.sub(
            lambda m: f"{m.group(1)}   # {light_min:.2f} / {dark_min:.2f}（各主题三档底色最低值，"
                      f"由 tools/contrast_audit.py --write 生成）", src)
    p.write_text(src, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
