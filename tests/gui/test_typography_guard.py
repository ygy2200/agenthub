# -*- coding: utf-8 -*-
"""gui 层：字号阶梯守卫（视觉打磨复盘 · 2026-10-15）。

复盘（离屏实测 18 页字号）发现 P2「页面标题 28→24」**只落地了 14 页**：
接入中心 / Agent 中心 / 随记 / 运营报告 4 页仍用 qfw 原生 TitleLabel(28px)。
原因和「4 页漏画底色」一模一样——改的时候靠**逐页手改**，没有遍历守卫，
于是"改了一半"不会报错、只会静默不齐（切页时页头字号跳变）。

本文件把两类契约钉死：
  · 页面不得出现 28px 的**短文本标题**（= qfw 原生 TitleLabel 当页头用）
  · 数字类标签必须是 metric 档（32px），不得退回 TitleLabel 的 28px
"""
from helpers import boot_window, pump

ALLOW_28PX = {"组件预览"}   # 预览页故意并列展示 qfw 原生 28px 作对照


def _px(lb) -> int:
    f = lb.font()
    return f.pixelSize() if f.pixelSize() > 0 else (f.pointSize() if f.pointSize() > 0 else -1)


def test_no_28px_page_titles(qapp, hub_root):
    """18 页里不得再用 qfw 原生 28px 当页头（组件预览页是对照展示，白名单）。"""
    from PySide6.QtWidgets import QLabel

    import actions
    import brain
    import tokens
    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 40), \
        "扫描未完成"

    bad = []
    for attr, title, *_rest in actions.PAGES:
        if title in ALLOW_28PX:
            continue
        page = actions.page_of(w, attr)
        if page is None:
            continue
        w.switchTo(page)
        for _ in range(5):
            qapp.processEvents()
        for lb in page.findChildren(QLabel):
            t = (lb.text() or "").strip()
            if not t or len(t) > 16 or "\n" in t:
                continue
            if _px(lb) == 28:
                bad.append(f"{title}:「{t[:12]}」28px")
                break
    assert not bad, (
        "页头/短标题仍是 qfw 原生 28px（应改用 uikit.PageTitleLabel(24) 或 _page_head）："
        f"{bad}")


def test_metric_numbers_are_32px(qapp, hub_root):
    """数字类读数必须是 metric 档 32px——不得退回与页头同级的 28px。"""
    import tokens
    import uikit

    card = uikit.NumberCard("测试")
    assert _px(card.value) == tokens.TYPE["metric"][0], \
        f"数字卡数值字号应为 {tokens.TYPE['metric'][0]}px，实际 {_px(card.value)}px"


def test_resolve_accepts_color_object():
    """tokens.resolve 收到色值 dict（而非名字字符串）时不得抛 TypeError。

    2026-10-15 复盘：`resolve(tokens.ACCENT)` 会撞进 `SEMANTIC.get(dict)` 抛
    "unhashable type: 'dict'"——入参问题伪装成内部错误，栈指不到调用方。
    现在走 C() 兜底解析。
    """
    import tokens

    for mode in ("dark", "light"):
        tokens.apply_theme(mode)
        try:
            v = tokens.resolve(tokens.ACCENT)      # 传常量本身（很自然的写法）
        except TypeError as e:
            raise AssertionError(f"resolve() 收到色值对象就抛 TypeError：{e}") from e
        assert v.startswith("#"), f"应解析出色值，实际 {v!r}"
        assert v == tokens.C(tokens.ACCENT), "resolve(dict) 应与 C(dict) 结果一致"


def test_qss_font_sizes_are_in_type_table():
    """源码 QSS 里的 font-size 必须落在 TYPE 表的字号集合内（第三轮新增）。

    根因：`chip_qss` 曾硬编码 `font-size:11px`，而 TYPE 表只有 12/14/16/20/24/28/32
    ——11px 是表外隐性第 7 档，离屏实测时间线页 700 个标签、大脑 26、统计 6 都吃这一档，
    改字号阶梯时它不跟随，字号探针也扫不到（探针读的是控件 font，不是 QSS）。
    现在 chip_qss 的字号由 TYPE["label"] 派生，本守卫负责防止别处再写出表外值。
    """
    import re
    from pathlib import Path
    import tokens

    ROOT = Path(__file__).resolve().parent.parent.parent
    sizes = {spec[0] for spec in tokens.TYPE.values()}
    # 豁免：report.py 是**图表 SVG 内部**的 CSS（条形图数值标签/轴标签），
    # 不是 UI 字号阶梯——窄条里放 12px 会挤爆，且它不进 QLabel 字体体系。
    # 豁免必须显式列名 + 理由，禁止用通配糊过去。
    EXEMPT = {"report.py"}
    files = ["uikit.py", "window.py", "palette.py"] + \
            [str(p) for p in sorted((ROOT / "pages").glob("*.py"))]
    bad = []
    for rel in files:
        if rel in EXEMPT:
            continue
        f = ROOT / rel
        if not f.exists():
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            body = line.split("  #")[0]
            if body.strip().startswith("#"):
                continue
            for m in re.finditer(r"font-size:\s*(\d+)px", body):
                if int(m.group(1)) not in sizes:
                    bad.append(f"{rel}:{i} font-size:{m.group(1)}px")
    assert not bad, (
        f"QSS 字号不在 TYPE 表（允许 {sorted(sizes)}）——请改用 tokens.TYPE 派生或登记新档：{bad}")


def test_badge_column_fits_longest_agent_name(qapp, hub_root):
    """徽章定宽列必须放得下最长的 agent 显示名（第三轮：字号 11→12/500 后加宽）。

    11px 时 BADGE_COL_W=76 刚好够；提到 12px/500 后 "WorkBuddy" 按生产字体实测需
    103px，不加宽就会被裁成半截名字（用户看到 "Claude C..."）。

    ⚠ 必须带 `qapp` fixture：badge() 内部要创建 QLabel，**没有 QApplication 时 Qt 直接
    abort（exit 127，无任何输出）**。2026-10-16 复盘实测：本测试最初漏了 fixture，
    只有当别的测试先建好 app 时才侥幸跑过；单独 `pytest path::test` 静默死亡，
    等于守卫在"单跑"场景下失效。带 fixture 后两种跑法都真跑。
    """
    from PySide6.QtGui import QFontMetrics
    import brain
    import tokens
    import uikit

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    f = QFontMetrics(uikit.badge("claude", "").font())
    for name in ("Claude Code", "WorkBuddy", "ZCode"):
        need = f.horizontalAdvance(name) + 2 * tokens.SPACE_S + 4   # padding + 余量
        assert uikit.BADGE_COL_W >= need, (
            f"徽章列宽 {uikit.BADGE_COL_W} 放不下「{name}」（需 {need}px）——"
            "请调大 uikit.BADGE_COL_W 或给徽章加 tooltip")
