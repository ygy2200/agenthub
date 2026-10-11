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
        lay.addWidget(self.seg)
        lay.addWidget(self.stack, 1)

