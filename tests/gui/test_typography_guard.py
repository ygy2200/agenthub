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
