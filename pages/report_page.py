# -*- coding: utf-8 -*-
"""ReportPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
class ReportPage(QWidget):
    """运营报告页：一键生成 mentor 式反馈报告（复用 report.generate_report，单文件 HTML），
    系统默认浏览器打开；历史报告列表可回看。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.addWidget(TitleLabel("运营报告"))
        lay.addWidget(CaptionLabel("大脑运营全景：你在做什么 / 摩擦点与修复 / 值得一试的能力——"
                                   "单文件 HTML 零 LLM 拼装，同一天重复生成会覆盖"))

        genCard = CardWidget()
        g = QHBoxLayout(genCard)
        g.setContentsMargins(20, 16, 20, 16)
        gt = QVBoxLayout()
        gt.addWidget(StrongBodyLabel("生成今日报告"))
        gt.addWidget(BodyLabel("从大脑数据库实时统计，浏览器打开；历史报告保留在同目录可回看。"))
        g.addLayout(gt, 1)
        self.genBtn = PrimaryPushButton(ic("SYNC"), "生成并打开")
        self.genBtn.clicked.connect(self.generate)
        g.addWidget(self.genBtn)
        lay.addWidget(genCard)

        histCard = CardWidget()
        hv = QVBoxLayout(histCard)
        hv.setContentsMargins(20, 16, 20, 16)
        hv.addWidget(StrongBodyLabel("历史报告（双击打开）"))
        self.histList = ListWidget()
        self.histList.itemDoubleClicked.connect(
            lambda it: os.startfile(it.data(Qt.UserRole)))  # noqa: S606
        hv.addWidget(self.histList, 1)
        lay.addWidget(histCard, 1)
        self.reload_reports()

    def reload_reports(self):
        self.histList.clear()
        d = Path(self.win.root) / "_hub" / "reports" if self.win.root else None
        # Round 5：没有报告目录/还没生成过报告时给引导（原来是一片空白）
        list_empty_guard(self.histList, self.histList.parentWidget().layout(),
                         "还没有运营报告",
                         "点「生成报告」产出一份 HTML（含近期待办/停滞项目/错误登记摘要），"
                         "生成后可双击打开")
        if not d or not d.is_dir():
            return
        for p in sorted(d.glob("反馈报告-*.html"), reverse=True):
            self.histList.addItem(p.stem)  # 既有模式：addItem(str) 后按行号 setData
            self.histList.item(self.histList.count() - 1).setData(Qt.UserRole, str(p))
        tight_rows(self.histList)

    def generate(self):
        if not self.win.root:
            InfoBar.warning("未选择根目录", "先到「设置」页选择根目录", duration=4000, parent=self)
            return
        self.genBtn.setEnabled(False)
        w = FnWorker(lambda: report.generate_report(self.win.root), self)
        w.done.connect(self._on_generated)
        self.win._workers.append(w)
        w.start()

    def _on_generated(self, result):
        self.genBtn.setEnabled(True)
        if isinstance(result, Exception):
            InfoBar.error("报告生成失败", str(result), duration=6000, parent=self)
            return
        self.reload_reports()
        os.startfile(result)  # noqa: S606
        InfoBar.success("报告已生成", result, duration=5000, parent=self)

