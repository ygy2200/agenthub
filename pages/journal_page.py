# -*- coding: utf-8 -*-
"""JournalPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
class JournalPage(QWidget):
    """流水页：错误登记（谁错了、怎么回滚、处理状态）+ 全部写操作流水（可撤销）。
    错误来源：MCP 工具执行异常自动落盘 + agent 主动 hub_report_error。"""

    ACTION_CN = {
        "log_work": "写工作记录", "undo_log_work": "撤销记录", "create_project": "新建项目",
        "memory_append": "写记忆", "memory_overwrite": "覆写记忆", "memory_edit": "编辑记忆",
        "edit": "编辑文件", "restore": "还原备份", "error_status": "流转错误状态",
        "bootstrap_inject": "注入引导", "bootstrap_remove": "移除引导",
        "mcp_connect": "接入MCP", "mcp_remove": "移除接入",
    }

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.errors: list = []
        self.journal: list = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)

        top = QHBoxLayout()
        top.addWidget(SubtitleLabel("流水 · 错误与操作"))
        top.addStretch(1)
        refreshBtn = PushButton(ic("SYNC", "INFO"), "刷新")
        refreshBtn.clicked.connect(self.reload)
        top.addWidget(refreshBtn)
        lay.addLayout(top)
        lay.addWidget(CaptionLabel("agent 执行出错（自动/主动上报）在这里可查可流转；"
                                   "每一次写操作（记录/记忆/接入/引导/还原）都有流水，记录类可撤销。"))

        # 列表区不包卡：ListWidget 自带边界，卡套列表=框中框
        g1 = QVBoxLayout()
        g1.setSpacing(8)
        g1.addWidget(StrongBodyLabel("错误登记（open=待处理 · fixed=已处理）"))
        self.errList = ListWidget()
        self.errList.setMinimumHeight(150)
        g1.addWidget(self.errList, 1)
        errRow = QHBoxLayout()
        self.fixBtn = PushButton("标记已修")
        self.reopenBtn = PushButton("重新打开")
        self.openProjBtn = PushButton(ic("FOLDER", "INFO"), "打开相关项目")
        for b in (self.fixBtn, self.reopenBtn, self.openProjBtn):
            errRow.addWidget(b)
        errRow.addStretch(1)
        g1.addLayout(errRow)
        lay.addLayout(g1, 2)

        g2 = QVBoxLayout()
        g2.setSpacing(8)
        g2.addWidget(StrongBodyLabel("操作流水（最新在上，最近 200 条）"))
        self.jList = ListWidget()
        g2.addWidget(self.jList, 1)
        jRow = QHBoxLayout()
        self.undoBtn = PushButton(ic("REPEAT", "INFO"), "撤销选中的工作记录")
        self.undoBtn.setToolTip("仅限各 agent 经 hub_log_work 写入的最新一条；原文会先自动备份")
        jRow.addWidget(self.undoBtn)
        jRow.addStretch(1)
        g2.addLayout(jRow)
        lay.addLayout(g2, 3)

        self.fixBtn.clicked.connect(lambda: self.set_status("fixed"))
        self.reopenBtn.clicked.connect(lambda: self.set_status("open"))
        self.openProjBtn.clicked.connect(self.open_project)
        self.undoBtn.clicked.connect(self.undo_selected)

    def reload(self):
        if not self.win.root:
            return
        self.errors = brain.error_list(self.win.root)
        self.errList.clear()
        for e in self.errors:
            self.errList.addItem(
                f"#{e['id']} [{e['status']}] {e['ts']}  {e['agent']}"
                f" · {e['project'] or '无项目'}：{e['title']}"
                + (f"  ｜回滚：{e['undo'][:60]}" if e["undo"] else ""))
        if not self.errors:
            self.errList.addItem("（无错误登记）")
        self.journal = brain.journal_list(self.win.root, 200)
        self.jList.clear()
        for e in self.journal:
            act = self.ACTION_CN.get(e["action"], e["action"])
            tgt = str(e["target"] or "")
            tgt = Path(tgt).name if "/" in tgt or "\\" in tgt else tgt
            self.jList.addItem(f"{e['ts']}  [{agent_disp(e['agent'])}]  {act}  {tgt}"
                               + (f"  {str(e['note'])[:70]}" if e["note"] else ""))
        if not self.journal:
            self.jList.addItem("（暂无操作流水——agent 接入引导后，它们的记录动作会出现在这里）")
        tight_rows(self.errList)
        tight_rows(self.jList)

    def set_status(self, status):
        row = self.errList.currentRow()
        if row < 0 or row >= len(self.errors):
            InfoBar.warning("先选择一条错误登记", "", duration=2000, parent=self.win)
            return
        e = self.errors[row]
        err = brain.error_set_status(self.win.root, e["id"], status)
        if err:
            op_failed("标记错误状态", err, parent=self.win)
        else:
            InfoBar.success("已更新", f"#{e['id']} → {status}", duration=2000, parent=self.win)
        self.reload()

    def open_project(self):
        row = self.errList.currentRow()
        if row < 0 or row >= len(self.errors):
            return
        p = self.errors[row].get("project", "")
        d = Path(self.win.root) / p if p else None
        if d and d.is_dir():
            os.startfile(str(d))  # noqa: S606

    def undo_selected(self):
        row = self.jList.currentRow()
        if row < 0 or row >= len(self.journal):
            InfoBar.warning("先选择一条操作流水", "", duration=2000, parent=self.win)
            return
        e = self.journal[row]
        if e["action"] != "log_work":
            InfoBar.warning("只能撤销「写工作记录」类型的操作", "", duration=2500, parent=self.win)
            return
        err, info = brain.undo_last_record(self.win.root, e["agent"])
        if err:
            InfoBar.error("撤销失败", err, duration=4000, parent=self.win)
        else:
            InfoBar.success("已撤销（软删，流水可追溯）", info, duration=3000, parent=self.win)
        self.reload()
        self.win.refresh()

