# -*- coding: utf-8 -*-
"""JournalPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

import re

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
        self._sel_err_id = 0
        # 2026-10-16：整页套滚动保护（#2055 修复模式）。此前无滚动，920×600 下
        # 最小需求高 588 > 可用 551（errList 150 + 两个 section_head + 两行按钮 +
        # 说明文字），layout_stress 从第一轮报"压扁风险"报到现在。
        # 内容天然长（流水 200 条），滚动比压缩更合理。
        _outer = QVBoxLayout(self)
        _outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollArea()
        self.scroll.setWidgetResizable(True)
        _body = QWidget()
        self.scroll.setWidget(_body)
        self.scroll.enableTransparentBackground()   # 必须在 setWidget 后（qfw API 需 widget 已存在才命中 body）
        _outer.addWidget(self.scroll)
        lay = QVBoxLayout(_body)
        set_page_margins(lay)

        top = QHBoxLayout()
        top.addWidget(SubtitleLabel("流水 · 错误与操作"))
        top.addStretch(1)
        refreshBtn = PushButton(ic("SYNC", "INFO"), "刷新")
        refreshBtn.clicked.connect(self.reload)
        top.addWidget(refreshBtn)
        lay.addLayout(top)
        # 2026-10-16：长说明文字开自动换行。此前不换行 → minimumSizeHint 宽 669px，
        # 把整页最小需求顶到 584px，920×600 窗口（扣导航后内容区仅 551px）下必压扁
        # （layout_stress 从第一轮报到现在）。
        _desc = CaptionLabel("agent 执行出错（自动/主动上报）在这里可查可流转；"
                             "每一次写操作（记录/记忆/接入/引导/还原）都有流水，记录类可撤销。")
        _desc.setWordWrap(True)
        lay.addWidget(_desc)

        # 列表区不包卡：ListWidget 自带边界，卡套列表=框中框
        g1 = QVBoxLayout()
        g1.setSpacing(tokens.SPACE_S)
        g1.addWidget(section_head("错误登记（未关闭=待处理 · 已修复=已处理）", "FLAG"))
        errTop = QHBoxLayout()
        self.errFilter = ComboBox()
        self.errFilter.addItems(["全部", "未关闭", "已修复"])
        self.errFilter.setFixedWidth(110)
        errTop.addWidget(self.errFilter)
        self.errCount = CaptionLabel("")
        errTop.addWidget(self.errCount)
        errTop.addStretch(1)
        g1.addLayout(errTop)
        self.errList = ListWidget()
        self.errList.setMinimumHeight(130)
        g1.addWidget(self.errList, 1)
        # 详情预览：回滚方式是错误登记最有价值的字段，此前只能看到列表里截断的 60 字
        self.errPreview = TextBrowser()
        self.errPreview.setMinimumHeight(110)
        g1.addWidget(self.errPreview)
        errRow = QHBoxLayout()
        self.fixBtn = PushButton("标记已修")
        self.reopenBtn = PushButton("重新打开")
        self.openProjBtn = PushButton(ic("FOLDER", "INFO"), "打开相关项目")
        self.copyUndoBtn = PushButton(ic("COPY", "INFO"), "复制回滚方式")
        for b in (self.fixBtn, self.reopenBtn, self.openProjBtn, self.copyUndoBtn):
            errRow.addWidget(b)
        errRow.addStretch(1)
        g1.addLayout(errRow)
        lay.addLayout(g1, 2)

        g2 = QVBoxLayout()
        g2.setSpacing(tokens.SPACE_S)
        g2.addWidget(section_head("操作流水（最新在上，最近 200 条）", "HISTORY"))
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
        self.copyUndoBtn.clicked.connect(self.copy_undo)
        self.undoBtn.clicked.connect(self.undo_selected)
        self.errFilter.currentIndexChanged.connect(self.reload)
        self.errList.currentRowChanged.connect(self._on_err_selected)
        self.errList.itemDoubleClicked.connect(lambda _: self.open_project())

    def reload(self):
        if not self.win.root:
            return
        key = {1: "open", 2: "fixed"}.get(self.errFilter.currentIndex(), "")
        self.errors = brain.error_list(self.win.root, key)
        self.errList.clear()
        for e in self.errors:
            st = "未关闭" if e["status"] == "open" else "已修复"
            self.errList.addItem(
                f"#{e['id']} [{st}] {e['ts']}  {e['agent']}"
                f" · {e['project'] or '无项目'}：{e['title']}"
                + (f"  ｜回滚：{e['undo'][:60]}" if e["undo"] else ""))
        if not self.errors:
            self.errList.addItem("（无错误登记）")
        self.errCount.setText(f"当前 {len(self.errors)} 条 · 未关闭 "
                              f"{sum(1 for e in self.errors if e['status'] == 'open')}")
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
        if self.errors:
            self.errList.setCurrentRow(self._row_of_err(self._sel_err_id))
        else:
            self._on_err_selected(-1)

    def _selected_error(self):
        row = self.errList.currentRow()
        if 0 <= row < len(self.errors):
            return self.errors[row]
        return None

    def _row_of_err(self, eid):
        for i, e in enumerate(self.errors):
            if e["id"] == eid:
                return i
        return 0

    def _on_err_selected(self, row):
        e = self._selected_error()
        self._sel_err_id = e["id"] if e else 0
        self._sync_err_actions(e)
        if not e:
            self.errPreview.setHtml(
                f"<p style='color:{tokens.C(tokens.TEXT_3)}'>点选上方错误查看详情与回滚方式</p>")
            return
        st = "未关闭" if e["status"] == "open" else "已修复"
        body = f"## {e['title']}\n\n**状态** {st} · **登记** {e['agent']} {e['ts']}"
        if e["project"]:
            body += f" · **项目** {e['project']}"
        if e["detail"]:
            body += f"\n\n{e['detail']}"
        if e["undo"]:
            body += f"\n\n**回滚方式**\n\n`{e['undo']}`"
        self.errPreview.setHtml(md_to_html(body))

    def _sync_err_actions(self, e=None):
        if e is None:
            e = self._selected_error()
        self.fixBtn.setEnabled(bool(e) and e["status"] == "open")
        self.reopenBtn.setEnabled(bool(e) and e["status"] == "fixed")
        self.openProjBtn.setEnabled(bool(e and e.get("project")))
        self.copyUndoBtn.setEnabled(bool(e and e.get("undo")))

    def set_status(self, status):
        e = self._selected_error()
        if not e:
            InfoBar.warning("先选中一条错误登记", "在「错误登记」列表里选中一条，再标记状态", duration=2000, parent=self.win)
            return
        err = brain.error_set_status(self.win.root, e["id"], status)
        if err:
            op_failed("标记错误状态", err, parent=self.win)
        else:
            InfoBar.success("已更新", f"#{e['id']} → {status}", duration=2000, parent=self.win)
        self.reload()

    def open_project(self):
        e = self._selected_error()
        if not e:
            return
        p = e.get("project", "")
        d = Path(self.win.root) / p if p else None
        if d and d.is_dir():
            os.startfile(str(d))  # noqa: S606

    def copy_undo(self):
        e = self._selected_error()
        if not e:
            InfoBar.warning("先选中一条错误登记", "在「错误登记」列表里选中一条，再复制回滚方式",
                            duration=2000, parent=self.win)
            return
        if not e.get("undo"):
            InfoBar.warning("这条错误没有回滚方式",
                            "让上报者用 hub_report_error(undo=…) 补齐，或按详情手动处理",
                            duration=3000, parent=self.win)
            return
        QApplication.clipboard().setText(e["undo"])
        InfoBar.success("已复制回滚方式", f"#{e['id']} · {e['title'][:40]}",
                        duration=2000, parent=self.win)

    def undo_selected(self):
        row = self.jList.currentRow()
        if row < 0 or row >= len(self.journal):
            InfoBar.warning("先选中一条操作流水", "在「操作流水」列表里选中一条，再撤销", duration=2000, parent=self.win)
            return
        e = self.journal[row]
        if e["action"] != "log_work":
            act = self.ACTION_CN.get(e["action"], e["action"])
            InfoBar.warning("只能撤销「写工作记录」",
                            f"选中的是「{act}」——流水里只有 agent 写工作记录可以撤销",
                            duration=2500, parent=self.win)
            return
        # Round 4：按**选中条目**的记录 id 撤销（原来调栈式的 undo_last_record(agent)，
        # 撤的永远是该 agent 最新一条 —— 选中较早的流水会静默撤掉另一条，操作错对象）。
        mt = re.search(r"records#(\d+)", str(e.get("target") or ""))
        if not mt:
            InfoBar.warning("这条流水没有可撤销的记录",
                            "目标里没有记录编号，可能不是记录写入动作", duration=2500, parent=self.win)
            return
        err, info = brain.undo_record(self.win.root, int(mt.group(1)))
        if err:
            InfoBar.error("撤销失败", err, duration=4000, parent=self.win)
        else:
            InfoBar.success("已撤销（软删，流水可追溯）", info, duration=3000, parent=self.win)
        self.reload()
        self.win.refresh()

