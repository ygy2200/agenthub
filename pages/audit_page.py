# -*- coding: utf-8 -*-
"""AuditPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
class AuditPage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        set_page_margins(lay)
        top = QHBoxLayout()
        top.addWidget(SubtitleLabel("对账中心"))
        top.addStretch(1)
        self.openBtn = PushButton(ic("FOLDER", "INFO"), "打开所在位置")
        top.addWidget(self.openBtn)
        self.copyBtn = PushButton(ic("COPY", "INFO"), "复制建议名")
        top.addWidget(self.copyBtn)
        lay.addLayout(top)
        lay.addWidget(CaptionLabel("发现不合规目录：agent 前缀平行目录 / 野目录 / 重复组 / 缺工作记录。"
                                   "本软件不自动改名，收编请人工确认后进行。"))
        self.listw = ListWidget()
        lay.addWidget(self.listw, 1)
        self.issues = []
        self.openBtn.clicked.connect(self.open_loc)
        self.copyBtn.clicked.connect(self.copy_suggest)

    def set_snapshot(self, snap):
        self.issues = snap.issues
        self.listw.clear()
        for i in snap.issues:
            self.listw.addItem(f"[{ISSUE_KIND_CN.get(i.kind, i.kind)}]  {i.detail}")
        # Round 5：无对账问题时给引导（原来是一片空白）
        list_empty_guard(self.listw, self.layout(),
                         "没有对账问题",
                         "目录命名、记录文件都在规范内；本软件只报告不自动改名")

    def current_issue(self):
        row = self.listw.currentRow()
        return self.issues[row] if 0 <= row < len(self.issues) else None

    def open_loc(self):
        it = self.current_issue()
        if it and Path(it.path).exists():
            os.startfile(it.path if Path(it.path).is_file() else str(Path(it.path).parent))  # noqa: S606

    def copy_suggest(self):
        it = self.current_issue()
        if it:
            QApplication.clipboard().setText(core.suggest_rename(Path(it.path).name))
            InfoBar.success("已复制建议名", "", duration=1500, parent=self.win)

