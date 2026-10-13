# -*- coding: utf-8 -*-
"""LedgerPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
from pages.audit_page import AuditPage  # noqa: F401
from pages.journal_page import JournalPage  # noqa: F401
class LedgerPage(QWidget):
    """流水·对账合并页：原「流水」与「对账」两页功能原样保留，由分段控件承载。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # 统一页头（Round 4 / 2026-10-13）：这是全库唯一没有页头的顶层页——
        # 其余 17 个顶层页都有「Title + 一句话定位语」，此页原本直接进分段控件。
        # 边距只给页头/分段控件（各 24），stack 仍为 0：子页 journal/audit 自带 24 边距，
        # 若容器再加 24 会变成 48，比其它页多缩进一档。
        _head = QWidget()
        _hl = QVBoxLayout(_head)
        _hl.setContentsMargins(24, 16, 24, 12)
        _hl.addWidget(_page_head(
            "流水·对账",
            "错误登记可标记流转、操作流水可撤销——谁在何时动了什么，一页查清并回滚"))
        lay.addWidget(_head)
        self.seg = SegmentedWidget(self)
        self.seg.addItem("journal", "错误与流水")
        self.seg.addItem("audit", "对账中心")
        self.journal = JournalPage(win)
        self.audit = AuditPage(win)
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.journal)
        self.stack.addWidget(self.audit)
        self.seg.currentItemChanged.connect(
            lambda key: self.stack.setCurrentIndex({"journal": 0, "audit": 1}.get(key, 0)))
        self.seg.setCurrentItem("journal")
        _segw = QWidget()
        _sl = QVBoxLayout(_segw)
        _sl.setContentsMargins(24, 0, 24, 8)
        _sl.addWidget(self.seg)
        lay.addWidget(_segw)
        lay.addWidget(self.stack, 1)

