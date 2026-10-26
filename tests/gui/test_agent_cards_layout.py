# -*- coding: utf-8 -*-
"""R169 Agent 中心卡片：状态行不得硬裁掉「最近活跃」时间。

真实截图里 ZCode 状态行只到「18:2」——3 位记录数时整行 243px，超过卡片 240px，
CaptionLabel 直接裁切且无 tooltip。改为缩短可见文案 + ElidedLabel 兜底。
"""
from types import SimpleNamespace

from helpers import boot_window, pump, settle


def _load_production_font():
    """测试进程默认字体与生产不同；不加载会让中文宽度失真（实测误报裁切）。"""
    import os
    from PySide6.QtGui import QFontDatabase
    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"):
        if os.path.exists(path):
            QFontDatabase.addApplicationFont(path)
            return


def _agent(name, skills=1, mcps=1, mems=1, cfgs=1):
    return SimpleNamespace(
        name=name,
        home=f"C:\\Users\\y\\.{name.lower()}",
        skills=[None] * skills,
        mcps=[None] * mcps,
        memories=[None] * mems,
        configs=[None] * cfgs,
    )


def _cards(hp):
    return [hp.cardsRow.itemAt(i).widget() for i in range(hp.cardsRow.count())
            if hp.cardsRow.itemAt(i).widget() is not None]


def test_agent_card_status_fits_without_hard_clip(qapp, hub_root):
    from uikit import AGENT_CARD_W, ElidedLabel
    _load_production_font()
    w = boot_window(qapp, hub_root)
    try:
        hp = w.hub_page
        w.switchTo(hp)
        assert pump(qapp, lambda: hasattr(hp, "_reg"), 30), "agent 探测未完成"
        hp.agents = [_agent("ZCode", 170, 6, 42), _agent("Claude", 34, 8, 22),
                     _agent("Big", 1, 1, 1), _agent("NoReg")]
        hp._reg = {
            "zcode": {"last_seen": "2026-10-25T18:28:00", "online": True, "records": 631},
            "claude": {"last_seen": "2026-10-25T17:28:00", "online": False, "records": 22},
            "big": {"last_seen": "2026-10-24T09:05:00", "online": True, "records": 12345678},
        }
        hp.rebuild_cards()
        settle(qapp, 1.5)
        cards = _cards(hp)
        assert len(cards) == 4
        assert all(c.width() == AGENT_CARD_W for c in cards)

        status = {}
        for card in cards:
            labels = [lb for lb in card.findChildren(ElidedLabel)
                      if lb.fullText().startswith(("●", "○"))]
            assert len(labels) == 1
            lb = labels[0]
            status[card.layout().itemAt(0).widget().text()] = lb

        z = status["ZCode"]
        assert z.fullText() == "● 在线 · 记录 631 条 · 最近 10-25 18:28"
        assert z.toolTip() == "在线 · 记录 631 条 · 最近活跃 10-25 18:28"
        assert z.text() == z.fullText(), "ZCode 状态行仍被裁"
        assert z.sizeHint().width() <= z.width() + 1, (z.sizeHint().width(), z.width())

        c = status["Claude"]
        assert c.toolTip().endswith("最近活跃 10-25 17:28")
        assert c.text() == c.fullText()

        # 超长记录数：允许省略号，但必须保留完整 tooltip，不能裸裁
        b = status["Big"]
        assert b.fullText().endswith("10-24 09:05")
        assert "最近活跃 10-24 09:05" in b.toolTip()
        assert b.text() == b.fullText() or b.text().endswith("…")

        nr = status["NoReg"]
        assert nr.fullText() == "○ 未接入 · 无心跳记录"
        assert "hub_heartbeat" in nr.toolTip()

        # 窄窗（920）卡片仍是定宽，状态行不因换行而裁字
        w.resize(920, 820)
        settle(qapp, 1.0)
        assert all(c.width() == AGENT_CARD_W for c in _cards(hp))
        assert status["ZCode"].text() == "● 在线 · 记录 631 条 · 最近 10-25 18:28"
    finally:
        w.hide()
