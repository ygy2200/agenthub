# -*- coding: utf-8 -*-
"""AgentHub GUI：项目 / 时间线 / 统计 / 搜索 / 对账 / 设置 六页。

数据全部来自 core.scan 现场扫描，无本地缓存，agent 直写目录后按 F5 即见。
"""
from __future__ import annotations

import ctypes
import html
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QShortcut, QKeySequence, QFont, QCursor
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit,
                               QVBoxLayout, QWidget, QHeaderView, QAbstractItemView,
                               QStackedWidget, QSizeGrip)
from qfluentwidgets import (BodyLabel, CaptionLabel, CardWidget, ComboBox, FluentIcon as FIF,
                            FluentWindow, InfoBar, LineEdit, ListWidget, MessageBoxBase,
                            PrimaryPushButton, ProgressBar, PushButton, ScrollArea,
                            SearchLineEdit, StrongBodyLabel, SubtitleLabel, TextBrowser,
                            TextEdit, TitleLabel, setTheme, Theme)

import agentscore
import core


def ic(name, fallback="INFO"):
    """图标枚举兜底，防不同版本枚举名缺失导致崩。"""
    return getattr(FIF, name, getattr(FIF, fallback))


AGENT_BADGE_COLOR = {
    "zcode": "#0078d4", "hermes": "#8764b8", "deepseek": "#0a7ea4",
    "claude": "#c76a42", "codex": "#4a6b8a", "其他": "#666666",
}
ISSUE_KIND_CN = {"prefix": "平行前缀目录", "wild": "野目录", "duplicate": "重复项目组", "no_record": "缺工作记录"}


# ---------------------------------------------------------------- Markdown 渲染（受限转换）

def _inline(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def md_to_html(text: str, limit: int = 100 * 1024) -> str:
    """把工作记录常用语法转 HTML：标题/列表/表格/粗体/行内码/代码块。"""
    text = text[:limit]
    blocks: list[str] = []
    code_buf: list[str] = []
    in_code = False
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            if in_code:
                blocks.append("<pre>" + html.escape("\n".join(code_buf)) + "</pre>")
                code_buf = []
            in_code = not in_code
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue
        blocks.append(line)
        i += 1
    if code_buf:
        blocks.append("<pre>" + html.escape("\n".join(code_buf)) + "</pre>")

    out: list[str] = []
    list_buf: list[str] = []
    table_buf: list[str] = []

    def flush_list():
        nonlocal list_buf
        if list_buf:
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in list_buf) + "</ul>")
            list_buf = []

    def flush_table():
        nonlocal table_buf
        if not table_buf:
            return
        rows = [r for r in table_buf if not re.fullmatch(r"\|[\s:\-|]+\|", r.strip())]
        html_rows = []
        for r in rows:
            cells = [c.strip() for c in r.strip().strip("|").split("|")]
            html_rows.append("<tr>" + "".join(f"<td>{_inline(html.escape(c))}</td>" for c in cells) + "</tr>")
        if html_rows:
            out.append('<table border="1" cellspacing="0" cellpadding="4">' + "".join(html_rows) + "</table>")
        table_buf = []

    for line in blocks:
        st = line.strip()
        if st.startswith("<pre>"):  # 第一遍生成的代码块，直接透传，勿再转义
            out.append(st)
            continue
        if st.startswith("|") and st.endswith("|"):
            flush_list()
            table_buf.append(st)
            continue
        flush_table()
        m = re.match(r"^(#{1,4})\s+(.*)$", st)
        if m:
            flush_list()
            lvl = len(m.group(1)) + 2
            out.append(f"<h{lvl}>{_inline(html.escape(m.group(2)))}</h{lvl}>")
            continue
        m = re.match(r"^[-*]\s+(.*)$", st)
        if m:
            list_buf.append(html.escape(m.group(1)))
            continue
        if st.startswith(">"):
            flush_list()
            out.append(f"<blockquote>{_inline(html.escape(st.lstrip('> ')))}</blockquote>")
            continue
        if not st:
            flush_list()
            continue
        flush_list()
        out.append(f"<p>{_inline(html.escape(st))}</p>")
    flush_list()
    flush_table()
    return "\n".join(out)


# ---------------------------------------------------------------- 后台线程

class ScanWorker(QThread):
    done = Signal(object)

    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = root

    def run(self):
        self.done.emit(core.scan(self.root))


class SearchWorker(QThread):
    done = Signal(list)

    def __init__(self, root, keyword, parent=None):
        super().__init__(parent)
        self.root, self.keyword = root, keyword

    def run(self):
        self.done.emit(core.search(self.root, self.keyword))


class FnWorker(QThread):
    """通用后台执行：run 任意无参函数，结果经信号回主线程。"""

    done = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:  # noqa: BLE001
            self.done.emit(e)


def open_location(path: str, select: bool = True):
    """在资源管理器中打开（select=True 时选中该文件/目录）。"""
    p = Path(path)
    if not p.exists():
        return
    if select and p.is_file():
        import subprocess
        subprocess.Popen(f'explorer /select,"{p}"')
    else:
        os.startfile(str(p) if p.is_dir() else str(p.parent))  # noqa: S606


class ClickBodyLabel(BodyLabel):
    clicked = Signal()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class RecordDetailDialog(MessageBoxBase):
    """时间线记录详情：标题 + 元信息 + 段正文渲染。"""

    def __init__(self, win, r):
        super().__init__(win)
        self.titleLabel = SubtitleLabel(r.title[:60])
        meta = QHBoxLayout()
        meta.addWidget(badge(r.agent, r.agent_raw))
        meta.addWidget(CaptionLabel(f"{r.date or '日期未标注'} · {r.project} · {r.line_no} 行起"))
        meta.addStretch(1)
        self.browser = TextBrowser()
        self.browser.setHtml(md_to_html(r.body or r.title))
        self.browser.setMinimumSize(860, 480)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addLayout(meta)
        self.viewLayout.addWidget(self.browser)
        openBtn = PushButton(ic("FOLDER", "INFO"), "打开项目目录")
        openBtn.clicked.connect(lambda: open_location(str(Path(win.root) / r.project), select=False))
        self.viewLayout.addWidget(openBtn, 0, Qt.AlignRight)
        self.yesButton.setText("关闭")
        self.cancelButton.hide()
        self.widget.setMinimumWidth(920)


# ---------------------------------------------------------------- 小组件

def badge(agent: str, raw: str) -> QLabel:
    text = {"zcode": "ZCode", "hermes": "hermes", "deepseek": "deepseek",
            "claude": "claude", "codex": "codex"}.get(agent, raw or "未标注")
    lb = QLabel(text)
    lb.setStyleSheet(
        f"color:white;background:{AGENT_BADGE_COLOR.get(agent, '#666666')};"
        "border-radius:8px;padding:1px 8px;font-size:11px;")
    lb.setFixedHeight(20)
    return lb


class NumberCard(CardWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 14, 20, 14)
        self.value = TitleLabel("0")
        self.title = CaptionLabel(title)
        lay.addWidget(self.value)
        lay.addWidget(self.title)


# ---------------------------------------------------------------- 新建项目 / Inbox 引入 对话框

class NewProjectDialog(MessageBoxBase):
    def __init__(self, parent):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel("新建项目")
        self.hint = CaptionLabel("命名须为「对象-问题」结构，例：KVK训枪-桌面图标异常")
        self.edit = LineEdit()
        self.edit.setPlaceholderText("对象-问题")
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.hint)
        self.viewLayout.addWidget(self.edit)
        self.yesButton.setText("创建")
        self.cancelButton.setText("取消")
        self.edit.textChanged.connect(self._check)
        self._check()

    def _check(self):
        err = core.validate_project_name(self.edit.text().strip())
        self.hint.setText(err or "命名合法，可以创建")
        self.hint.setStyleSheet("color:#d13438;" if err else "color:#107c10;")
        self.yesButton.setEnabled(not err)

    def name(self):
        return self.edit.text().strip()


class InboxPickDialog(MessageBoxBase):
    def __init__(self, parent, files):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel("从 00_Inbox 引入")
        self.hint = CaptionLabel("勾选要移入本项目 input\\ 的文件")
        self.listw = ListWidget()
        self.listw.addItems(files)
        self.listw.setSelectionMode(QAbstractItemView.MultiSelection)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.hint)
        self.viewLayout.addWidget(self.listw)
        self.yesButton.setText("引入")
        self.cancelButton.setText("取消")
        self.yesButton.setEnabled(bool(files))

    def picked(self):
        return [i.text() for i in self.listw.selectedItems()]


class EditAssetDialog(MessageBoxBase):
    """能力中心资产编辑器：记忆 / 规则 / 全局配置等文本文件（保存走自动备份）。"""

    def __init__(self, win, path, name):
        super().__init__(win)
        self.titleLabel = SubtitleLabel(f"编辑：{name}")
        self.edit = TextEdit()
        p = Path(path)
        self.edit.setPlainText(core.read_text(p) if p.is_file() else "")
        self.edit.setMinimumSize(820, 460)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.edit)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(860)

    def text(self):
        return self.edit.toPlainText()


# ---------------------------------------------------------------- 页面

class ProjectPage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.snap = None
        self.current = None
        root = QHBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)

        left = QVBoxLayout()
        self.filterEdit = SearchLineEdit()
        self.filterEdit.setPlaceholderText("过滤项目…")
        self.listw = ListWidget()
        left.addWidget(self.filterEdit)
        left.addWidget(self.listw, 1)
        btnrow = QHBoxLayout()
        self.newBtn = PrimaryPushButton(ic("ADD", "INFO"), "新建项目")
        self.refreshBtn = PushButton(ic("SYNC", "INFO"), "刷新")
        btnrow.addWidget(self.newBtn)
        btnrow.addWidget(self.refreshBtn)
        left.addLayout(btnrow)

        right = QVBoxLayout()
        head = QHBoxLayout()
        head.addStretch(1)
        self.openBtn = PushButton(ic("FOLDER", "INFO"), "打开目录")
        self.copyBtn = PushButton(ic("COPY", "INFO"), "复制路径")
        self.inboxBtn = PushButton(ic("DOWNLOAD", "INFO"), "从 Inbox 引入")
        for b in (self.inboxBtn, self.copyBtn, self.openBtn):
            head.addWidget(b)
        self.pathLabel = CaptionLabel("")
        self.issueLabel = BodyLabel("")
        self.issueLabel.setStyleSheet("color:#d13438;")

        self.browser = TextBrowser()
        self.browser.setHtml("<p style='color:#888'>左侧选择项目查看工作记录</p>")

        file_col = QVBoxLayout()
        file_col.addWidget(CaptionLabel("项目文件总览（历史项目无 input/output 时按实际位置分组 · 双击打开所在位置）"))
        self.fileList = ListWidget()
        self.fileList.itemDoubleClicked.connect(self.open_file_loc)

        self.nameLabel = TitleLabel("—")
        right.addWidget(self.nameLabel)
        right.addLayout(head)
        right.addWidget(self.pathLabel)
        right.addWidget(self.issueLabel)
        right.addWidget(self.browser, 1)
        right.addLayout(file_col)

        root.addLayout(left, 1)
        root.addLayout(right, 2)

        self.listw.currentRowChanged.connect(self.on_select)
        self.filterEdit.textChanged.connect(self.rebuild_list)
        self.openBtn.clicked.connect(self.open_dir)
        self.copyBtn.clicked.connect(self.copy_path)
        self.newBtn.clicked.connect(self.new_project)
        self.inboxBtn.clicked.connect(self.pick_inbox)
        self.refreshBtn.clicked.connect(self.win.refresh)

    # ---- 数据
    def set_snapshot(self, snap):
        self.snap = snap
        selected = self.current
        names = [p.name for p in snap.projects]
        self.rebuild_list()
        if selected in names:
            self.listw.setCurrentRow(names.index(selected))
        elif self.listw.count():
            self.listw.setCurrentRow(0)

    def rebuild_list(self):
        kw = self.filterEdit.text().strip().lower()
        self.listw.blockSignals(True)
        self.listw.clear()
        if self.snap:
            for p in self.snap.projects:
                if kw in p.name.lower():
                    self.listw.addItem(p.name)
        self.listw.blockSignals(False)
        if self.listw.count():
            self.listw.setCurrentRow(0)

    def on_select(self, row):
        if not self.snap or row < 0 or row >= self.listw.count():
            return
        name = self.listw.item(row).text()
        proj = next((p for p in self.snap.projects if p.name == name), None)
        self.current = name
        if not proj:
            return
        self.nameLabel.setText(proj.name)
        self.pathLabel.setText(proj.path)
        self.issueLabel.setText("⚠ " + "；".join(proj.issues) if proj.issues else "")
        if proj.doc_path:
            md = core.read_text(Path(proj.doc_path))
            self.browser.setHtml(md_to_html(md))
        else:
            self.browser.setHtml("<p style='color:#888'>无 工作记录.md，也无可用 md/txt 主文档</p>")
        self.fileList.clear()
        for group, rp, fp in proj.files:
            self.fileList.addItem(f"[{group}]  {rp}")
            self.fileList.item(self.fileList.count() - 1).setData(Qt.UserRole, fp)
        if not proj.files:
            self.fileList.addItem("（空目录）")

    def open_file_loc(self, item):
        fp = item.data(Qt.UserRole)
        if fp:
            open_location(fp)

    def open_dir(self):
        if self.current:
            p = Path(self.win.root) / self.current
            if p.is_dir():
                os.startfile(str(p))  # noqa: S606

    def copy_path(self):
        if self.current:
            QApplication.clipboard().setText(str(Path(self.win.root) / self.current))
            InfoBar.success("已复制", "", duration=1500, parent=self.win)

    def new_project(self):
        if not self.win.root:
            InfoBar.warning("未设置根目录", "请先到设置页选择", duration=2500, parent=self.win)
            return
        dlg = NewProjectDialog(self.win)
        if dlg.exec():
            err = core.create_project(self.win.root, dlg.name())
            if err:
                InfoBar.error("创建失败", err, duration=4000, parent=self.win)
            else:
                InfoBar.success("已创建", dlg.name(), duration=2000, parent=self.win)
                self.win.refresh()

    def pick_inbox(self):
        if not self.current or not self.snap:
            return
        files = core.scan(self.win.root).inbox if self.win.root else []
        if not files:
            InfoBar.info("00_Inbox 是空的", "", duration=2000, parent=self.win)
            return
        dlg = InboxPickDialog(self.win, files)
        if dlg.exec():
            errs = [e for e in (core.move_to_project(self.win.root, f, self.current) for f in dlg.picked()) if e]
            if errs:
                InfoBar.error("部分失败", errs[0], duration=4000, parent=self.win)
            else:
                InfoBar.success("已引入", "", duration=2000, parent=self.win)
                self.win.refresh()

    def select_project(self, name):
        names = [self.listw.item(i).text() for i in range(self.listw.count())]
        if name in names:
            self.listw.setCurrentRow(names.index(name))


AGENT_CN = {"zcode": "ZCode", "hermes": "hermes", "deepseek": "deepseek",
            "claude": "claude", "codex": "codex", "其他": "其他"}


class TimelinePage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.all_entries: list = []
        self.projects: list = []
        self.render_limit = 80  # 懒加载：首屏渲染条数
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 0)
        top = QHBoxLayout()
        top.addWidget(SubtitleLabel("跨项目时间线"))
        top.addStretch(1)
        top.addWidget(CaptionLabel("agent"))
        self.agentFilter = ComboBox()
        self.agentFilter.setFixedWidth(130)
        top.addWidget(self.agentFilter)
        top.addWidget(CaptionLabel("项目"))
        self.projFilter = ComboBox()
        self.projFilter.setFixedWidth(220)
        top.addWidget(self.projFilter)
        lay.addLayout(top)
        tip = CaptionLabel("按时间倒序 · 点击条目查看完整操作步骤 · 点击项目名跳转")
        lay.addWidget(tip)
        self.scroll = ScrollArea()
        self.container = QWidget()
        self.box = QVBoxLayout(self.container)
        self.box.setContentsMargins(4, 12, 16, 24)
        self.box.setSpacing(8)
        self.scroll.setWidget(self.container)
        self.scroll.setWidgetResizable(True)
        self.scroll.enableTransparentBackground()
        lay.addWidget(self.scroll, 1)
        self.agentFilter.currentIndexChanged.connect(self.rebuild)
        self.projFilter.currentIndexChanged.connect(self.rebuild)

    def set_snapshot(self, snap):
        self.projects = snap.projects
        entries = []
        for p in snap.projects:
            entries.extend(p.records)
        entries.sort(key=lambda r: r.date, reverse=True)
        self.all_entries = entries

        # 过滤器填充（保持当前选择）
        agents = ["全部"] + [AGENT_CN.get(a, a) for a in sorted(snap.agent_counts.keys())]
        projs = ["全部"] + [p.name for p in snap.projects]
        for combo, items in ((self.agentFilter, agents), (self.projFilter, projs)):
            combo.blockSignals(True)
            cur = combo.currentText()
            combo.clear()
            combo.addItems(items)
            idx = combo.findText(cur)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)
        self.render_limit = 80  # 新快照重置懒加载
        self.rebuild()

    def _filtered(self):
        a = self.agentFilter.currentText()
        pj = self.projFilter.currentText()
        out = self.all_entries
        if a and a != "全部":
            want = {v: k for k, v in AGENT_CN.items()}.get(a, a.lower())
            out = [r for r in out if r.agent == want]
        if pj and pj != "全部":
            out = [r for r in out if r.project == pj]
        return out

    def rebuild(self):
        while self.box.count():
            item = self.box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        entries = self._filtered()
        # 懒加载：一次只渲染一屏多一点的条目，避免大量控件卡主线程
        shown = entries[:self.render_limit]
        cur_date = None
        card_lay = None
        for r in shown:
            d = r.date or "日期未标注"
            if d != cur_date:
                cur_date = d
                card = CardWidget()
                card_lay = QVBoxLayout(card)
                card_lay.setContentsMargins(18, 12, 18, 12)
                card_lay.setSpacing(6)
                card_lay.addWidget(StrongBodyLabel(d))
                self.box.addWidget(card)
            row = QHBoxLayout()
            row.addWidget(badge(r.agent, r.agent_raw))
            title = ClickBodyLabel(r.title[:80])
            title.setToolTip(f"{r.project} · {r.title}\n点击查看完整记录")
            title.setCursor(Qt.PointingHandCursor)
            title.clicked.connect(lambda _, rr=r: self.show_detail(rr))
            proj_btn = PushButton(r.project[:18])
            proj_btn.setFixedHeight(26)
            proj_btn.setToolTip(r.project)
            proj_btn.clicked.connect(lambda _, n=r.project: self.jump(n))
            row.addWidget(title, 1)
            row.addWidget(proj_btn)
            card_lay.addLayout(row)
        if not shown:
            tip = BodyLabel("暂无符合条件的记录")
            tip.setAlignment(Qt.AlignCenter)
            self.box.addWidget(tip)
        elif len(entries) > len(shown):
            more = PushButton(f"加载更多（还有 {len(entries) - len(shown)} 条）")
            more.clicked.connect(self.load_more)
            wrap = QHBoxLayout()
            wrap.addStretch(1)
            wrap.addWidget(more)
            wrap.addStretch(1)
            self.box.addLayout(wrap)
            self.box.addWidget(CaptionLabel(f"共 {len(entries)} 条，已显示 {len(shown)} 条；用上方过滤器可缩小范围"))

    def load_more(self):
        self.render_limit += 80
        self.rebuild()

    def show_detail(self, r):
        dlg = RecordDetailDialog(self.win, r)
        dlg.exec()

    def jump(self, name):
        self.win.project_page.select_project(name)
        self.win.switchTo(self.win.project_page)


class StatsPage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        row = QHBoxLayout()
        self.cardProj = NumberCard("项目总数")
        self.cardRec = NumberCard("工作记录条数")
        self.cardIssue = NumberCard("对账问题数")
        for c in (self.cardProj, self.cardRec, self.cardIssue):
            row.addWidget(c)
        lay.addLayout(row)

        self.agentBox = QVBoxLayout()
        self.monthBox = QVBoxLayout()
        for box, t in ((self.agentBox, "各 agent 记录分布"), (self.monthBox, "近 6 个月活跃")):
            gb = CardWidget()
            g = QVBoxLayout(gb)
            g.setContentsMargins(20, 14, 20, 14)
            g.addWidget(StrongBodyLabel(t))
            g.addLayout(box)
            lay.addWidget(gb)
        lay.addStretch(1)

    def set_snapshot(self, snap):
        self.cardProj.value.setText(str(len(snap.projects)))
        self.cardRec.value.setText(str(snap.total_records))
        self.cardIssue.value.setText(str(len(snap.issues)))

        def clear(box):
            while box.count():
                it = box.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()

        clear(self.agentBox)
        total = max(1, snap.total_records)
        for agent, n in sorted(snap.agent_counts.items(), key=lambda x: -x[1]):
            row = QHBoxLayout()
            row.addWidget(badge(agent, agent))
            bar = ProgressBar()
            bar.setValue(round(n / total * 100))
            row.addWidget(bar, 1)
            row.addWidget(BodyLabel(str(n)))
            self.agentBox.addLayout(row)
        if not snap.agent_counts:
            self.agentBox.addWidget(CaptionLabel("暂无数据"))

        clear(self.monthBox)
        months = sorted(snap.monthly_counts.items())[-6:]
        mx = max((v for _, v in months), default=1)
        for m, n in months:
            row = QHBoxLayout()
            row.addWidget(BodyLabel(m))
            bar = ProgressBar()
            bar.setValue(round(n / mx * 100))
            row.addWidget(bar, 1)
            row.addWidget(BodyLabel(str(n)))
            self.monthBox.addLayout(row)
        if not months:
            self.monthBox.addWidget(CaptionLabel("暂无数据"))


class SearchPage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        self.edit = SearchLineEdit()
        self.edit.setPlaceholderText("搜索工作记录 / 文件名，回车执行…")
        self.edit.setClearButtonEnabled(True)
        lay.addWidget(self.edit)
        self.result = ListWidget()
        lay.addWidget(self.result, 1)
        self.preview = TextBrowser()
        self.preview.setMaximumHeight(220)
        lay.addWidget(self.preview)
        self.hits = []
        self.worker = None
        self._workers = []  # 持住运行中线程引用，防 GC 崩进程
        self.edit.returnPressed.connect(self.run)
        self.result.currentRowChanged.connect(self.show_hit)

    def run(self):
        kw = self.edit.text().strip()
        if not kw or not self.win.root:
            return
        self.result.clear()
        self.preview.setHtml("")
        self.result.addItem("搜索中…")
        w = SearchWorker(self.win.root, kw, self)
        w.done.connect(self.on_done)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def on_done(self, hits):
        self.hits = hits
        self.result.clear()
        for h in hits:
            self.result.addItem(f"{h.line}   —— {Path(h.path).name}:{h.line_no}")
        if not hits:
            self.result.addItem("无结果")

    def show_hit(self, row):
        if row < 0 or row >= len(self.hits):
            return
        h = self.hits[row]
        md = core.read_text(Path(h.path))
        self.preview.setHtml(md_to_html(md, limit=100 * 1024))


class AuditPage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
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

        errCard = CardWidget()
        g1 = QVBoxLayout(errCard)
        g1.setContentsMargins(18, 12, 18, 12)
        g1.setSpacing(6)
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
        lay.addWidget(errCard, 2)

        jCard = CardWidget()
        g2 = QVBoxLayout(jCard)
        g2.setContentsMargins(18, 12, 18, 12)
        g2.setSpacing(6)
        g2.addWidget(StrongBodyLabel("操作流水（最新在上，最近 200 条）"))
        self.jList = ListWidget()
        g2.addWidget(self.jList, 1)
        jRow = QHBoxLayout()
        self.undoBtn = PushButton(ic("REPEAT", "INFO"), "撤销选中的工作记录")
        self.undoBtn.setToolTip("仅限各 agent 经 hub_log_work 写入的最新一条；原文会先自动备份")
        jRow.addWidget(self.undoBtn)
        jRow.addStretch(1)
        g2.addLayout(jRow)
        lay.addWidget(jCard, 3)

        self.fixBtn.clicked.connect(lambda: self.set_status("fixed"))
        self.reopenBtn.clicked.connect(lambda: self.set_status("open"))
        self.openProjBtn.clicked.connect(self.open_project)
        self.undoBtn.clicked.connect(self.undo_selected)

    def reload(self):
        if not self.win.root:
            return
        self.errors = core.list_errors(self.win.root)
        self.errList.clear()
        for e in self.errors:
            self.errList.addItem(
                f"#{e.get('id')} [{e.get('status')}] {e.get('ts', '')}  {e.get('agent')}"
                f" · {e.get('project') or '无项目'}：{e.get('title')}"
                + (f"  ｜回滚：{e['undo'][:60]}" if e.get("undo") else ""))
        if not self.errors:
            self.errList.addItem("（无错误登记）")
        self.journal = core.read_journal(self.win.root, limit=200)
        self.jList.clear()
        for e in reversed(self.journal):
            act = self.ACTION_CN.get(e.get("action"), e.get("action", ""))
            tgt = Path(e.get("target", "") or "").name
            self.jList.addItem(f"{e.get('ts', '')}  [{e.get('agent')}]  {act}  {tgt}"
                               + (f"  {str(e.get('note'))[:70]}" if e.get("note") else ""))
        if not self.journal:
            self.jList.addItem("（暂无操作流水——agent 接入引导后，它们的记录动作会出现在这里）")

    def set_status(self, status):
        row = self.errList.currentRow()
        if row < 0 or row >= len(self.errors):
            InfoBar.warning("先选择一条错误登记", "", duration=2000, parent=self.win)
            return
        e = self.errors[row]
        err = core.set_error_status(self.win.root, e.get("id"), status)
        if err:
            InfoBar.error("操作失败", err, duration=4000, parent=self.win)
        else:
            InfoBar.success("已更新", f"#{e.get('id')} → {status}", duration=2000, parent=self.win)
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
        e = self.journal[len(self.journal) - 1 - row]  # 列表最新在上，映射回原序
        if e.get("action") != "log_work":
            InfoBar.warning("只能撤销「写工作记录」类型的操作", "", duration=2500, parent=self.win)
            return
        agent = e.get("agent", "")
        undone = {x.get("note") for x in self.journal if x.get("action") == "undo_log_work"}
        latest = next((x for x in reversed(self.journal)
                       if x.get("agent") == agent and x.get("action") == "log_work"
                       and x.get("note") not in undone), None)
        if latest is None or latest.get("ts") != e.get("ts") or latest.get("note") != e.get("note"):
            InfoBar.warning("只能撤销该 agent 的最新一条记录",
                            "先撤后面那条，再回来撤这条", duration=3000, parent=self.win)
            return
        err, bak = core.undo_log(self.win.root, agent)
        if err:
            InfoBar.error("撤销失败", err, duration=4000, parent=self.win)
        else:
            InfoBar.success("已撤销（原文已备份）", bak, duration=3000, parent=self.win)
        self.reload()
        self.win.refresh()


class OverviewPage(QWidget):
    """总览首页：打开软件第一眼看到全局——今天谁在干活、有多少待处理、agent 阵容。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.recent: list = []
        self.snap = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)

        head = QHBoxLayout()
        self.hello = TitleLabel("总览")
        head.addWidget(self.hello)
        head.addStretch(1)
        self.quickNew = PrimaryPushButton(ic("ADD", "INFO"), "新建项目")
        self.quickNew.clicked.connect(lambda: self.win.project_page.new_project())
        head.addWidget(self.quickNew)
        lay.addLayout(head)
        lay.addWidget(CaptionLabel("电脑上所有 agent 的工作入口，数据实时来自目录扫描（F5 刷新）"))

        row = QHBoxLayout()
        self.cardProj = NumberCard("项目总数")
        self.cardRec = NumberCard("工作记录条数")
        self.cardToday = NumberCard("今日动态")
        self.cardTodo = NumberCard("待处理（Inbox+对账）")
        for c in (self.cardProj, self.cardRec, self.cardToday, self.cardTodo):
            row.addWidget(c)
        lay.addLayout(row)

        cols = QHBoxLayout()
        # 左：最近 agent 动态
        recentCard = CardWidget()
        g1 = QVBoxLayout(recentCard)
        g1.setContentsMargins(18, 12, 18, 12)
        g1.setSpacing(8)
        g1.addWidget(StrongBodyLabel("最近动态（点击看详情）"))
        self.recentBox = QVBoxLayout()
        self.recentBox.setSpacing(4)
        g1.addLayout(self.recentBox)
        g1.addStretch(1)
        cols.addWidget(recentCard, 3)
        # 右：agent 阵容 + 快捷入口
        rightCol = QVBoxLayout()
        agentCard = CardWidget()
        g2 = QVBoxLayout(agentCard)
        g2.setContentsMargins(18, 12, 18, 12)
        g2.setSpacing(6)
        g2.addWidget(StrongBodyLabel("Agent 阵容（能力中心探测）"))
        self.agentBox = QVBoxLayout()
        self.agentBox.setSpacing(2)
        g2.addLayout(self.agentBox)
        g2.addWidget(StrongBodyLabel("当前活跃会话（hub_heartbeat 上报）"))
        self.sessionBox = QVBoxLayout()
        self.sessionBox.setSpacing(2)
        g2.addLayout(self.sessionBox)
        g2.addStretch(1)
        rightCol.addWidget(agentCard, 2)
        quickCard = CardWidget()
        g3 = QVBoxLayout(quickCard)
        g3.setContentsMargins(18, 12, 18, 12)
        g3.setSpacing(6)
        g3.addWidget(StrongBodyLabel("快捷入口"))
        for text, target in (("打开时间线", "timeline"), ("打开能力中心", "hub"),
                             ("检查对账问题", "audit"), ("搜索全部记录", "search")):
            b = PushButton(text)
            b.clicked.connect(lambda _, t=target: self.goto(t))
            g3.addWidget(b)
        rightCol.addWidget(quickCard, 1)
        cols.addLayout(rightCol, 2)
        lay.addLayout(cols)
        lay.addStretch(1)

    def goto(self, key):
        page = {"timeline": self.win.timeline_page, "hub": self.win.hub_page,
                "audit": self.win.audit_page, "search": self.win.search_page}[key]
        self.win.switchTo(page)

    def set_snapshot(self, snap):
        self.snap = snap
        today = date.today().isoformat()
        entries = sorted((r for p in snap.projects for r in p.records),
                         key=lambda r: r.date, reverse=True)
        self.recent = entries[:8]
        self.cardProj.value.setText(str(len(snap.projects)))
        self.cardRec.value.setText(str(snap.total_records))
        self.cardToday.value.setText(str(sum(1 for r in entries if r.date == today)))
        self.cardTodo.value.setText(str(len(snap.issues) + len(snap.inbox)))

        # 问候语
        h = datetime.now().hour
        greet = "早上好" if h < 12 else ("下午好" if h < 18 else "晚上好")
        self.hello.setText(f"{greet}，{today}")

        while self.recentBox.count():
            it = self.recentBox.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for r in self.recent:
            row = QHBoxLayout()
            row.addWidget(badge(r.agent, r.agent_raw))
            t = ClickBodyLabel(r.title[:52])
            t.setCursor(Qt.PointingHandCursor)
            t.clicked.connect(lambda _, rr=r: RecordDetailDialog(self.win, rr).exec())
            row.addWidget(t, 1)
            pn = CaptionLabel(r.project[:14])
            pn.setToolTip(r.project)
            row.addWidget(pn)
            wrap = QWidget()
            wrap.setLayout(row)
            self.recentBox.addWidget(wrap)
        if not self.recent:
            tip = CaptionLabel("暂无记录")
            self.recentBox.addWidget(tip)

    def set_agents(self, agents):
        while self.agentBox.count():
            it = self.agentBox.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for a in agents:
            line = CaptionLabel(f"{'●' if a.detected else '○'} {a.name}：技能 {len(a.skills)} · MCP {len(a.mcps)} · 记忆 {len(a.memories)}")
            line.setToolTip(a.home or "未检测到，可在能力中心手动添加目录")
            self.agentBox.addWidget(line)

    def set_sessions(self, sessions):
        while self.sessionBox.count():
            it = self.sessionBox.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not sessions:
            self.sessionBox.addWidget(CaptionLabel("（暂无——agent 开工调 hub_heartbeat 后显示）"))
            return
        for s in sessions[:6]:
            self.sessionBox.addWidget(CaptionLabel(
                f"● {s.get('agent')} → {s.get('project') or '未指定'}  {s.get('note') or ''}"))


class HubPage(QWidget):
    """能力中心：聚合电脑上各 agent 的技能 / MCP / 记忆 / 全局配置（只读）。"""

    VIEWS = ["技能库", "MCP 服务器", "记忆", "全局配置", "技能市场"]

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.agents: list = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)

        top = QHBoxLayout()
        top.addWidget(SubtitleLabel("能力中心"))
        top.addStretch(1)
        addBtn = PushButton(ic("ADD", "INFO"), "添加 agent 目录")
        addBtn.clicked.connect(self.add_agent)
        top.addWidget(addBtn)
        rescanBtn = PrimaryPushButton(ic("SYNC", "INFO"), "重新探测")
        rescanBtn.clicked.connect(self.rescan)
        top.addWidget(rescanBtn)
        lay.addLayout(top)
        lay.addWidget(CaptionLabel("自动探测本机 agent 及其技能 / MCP / 记忆 / 全局配置，只读聚合，绝不改动对方配置；"
                                   "未列出的 agent 可手动添加根目录。"))

        self.cardsRow = QHBoxLayout()
        self.cardsRow.setSpacing(10)
        lay.addLayout(self.cardsRow)

        viewRow = QHBoxLayout()
        viewRow.addWidget(CaptionLabel("资产视图"))
        self.viewCombo = ComboBox()
        self.viewCombo.addItems(self.VIEWS)
        self.viewCombo.setFixedWidth(160)
        viewRow.addWidget(self.viewCombo)
        viewRow.addStretch(1)
        self.skillFilter = SearchLineEdit()
        self.skillFilter.setPlaceholderText("过滤技能…")
        self.skillFilter.setFixedWidth(260)
        self.skillFilter.textChanged.connect(self.fill_skills)
        viewRow.addWidget(self.skillFilter)
        lay.addLayout(viewRow)

        self.stack = QStackedWidget()
        self.skillList = ListWidget()
        self.mcpList = ListWidget()
        self.memList = ListWidget()
        self.cfgList = ListWidget()
        self.marketList = ListWidget()
        for w in (self.skillList, self.mcpList, self.memList, self.cfgList, self.marketList):
            self.stack.addWidget(w)
            w.itemClicked.connect(self.on_preview)
            w.itemDoubleClicked.connect(self.on_open)
        self.viewCombo.currentIndexChanged.connect(self.stack.setCurrentIndex)
        lay.addWidget(self.stack, 1)

        marketRow = QHBoxLayout()
        self.marketTargets = ComboBox()
        self.marketTargets.setFixedWidth(220)
        installBtn = PrimaryPushButton(ic("DOWNLOAD", "INFO"), "安装选中技能")
        installBtn.clicked.connect(self.install_selected)
        marketRow.addWidget(self.marketTargets)
        marketRow.addWidget(installBtn)
        marketRow.addStretch(1)
        self.marketPage = QWidget()
        self.marketPage.setLayout(marketRow)
        self.stack.insertWidget(4, self.marketPage)
        self.marketList.setParent(None)  # 列表放进市场页上方
        mk = QVBoxLayout(self.marketPage)
        mk.insertWidget(0, CaptionLabel("技能来自 anthropics/skills 官方索引（hermes 本地缓存）。"
                                        "安装即从 GitHub 下载到目标技能库（需网络，走本机 7890 代理或直连）。"))
        mk.insertWidget(1, self.marketList, 1)
        self.stack.insertWidget(4, self.marketPage)

        lay.addWidget(CaptionLabel("单击下方预览 · 双击打开所在目录 · MCP 出于安全只显示名称与来源，不显示密钥内容"))

        editRow = QHBoxLayout()
        self.editBtn = PushButton(ic("EDIT", "INFO"), "编辑并保存（自动备份）")
        self.editBtn.setEnabled(False)
        self.editBtn.clicked.connect(self.edit_current)
        editRow.addWidget(self.editBtn)
        editRow.addStretch(1)
        editRow.addWidget(CaptionLabel("历史备份"))
        self.bakCombo = ComboBox()
        self.bakCombo.setFixedWidth(320)
        editRow.addWidget(self.bakCombo)
        self.restoreBtn = PushButton(ic("SYNC", "INFO"), "还原选中备份")
        self.restoreBtn.setEnabled(False)
        self.restoreBtn.clicked.connect(self.restore_memory)
        editRow.addWidget(self.restoreBtn)
        lay.addLayout(editRow)

        self.preview = TextBrowser()
        self.preview.setMaximumHeight(240)
        self.preview.setHtml("<p style='color:#888'>点击左侧列表项预览</p>")
        lay.addWidget(self.preview)

        self._workers = []
        QTimer.singleShot(300, self.rescan)

    # ---- 数据
    def rescan(self):
        extra = core.load_config().get("extra_agents", {})
        w = FnWorker(lambda: agentscore.detect_agents(extra), self)
        w.done.connect(self._on_scan)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()
        self.preview.setHtml("<p style='color:#888'>探测中…</p>")

    def _on_scan(self, result):
        if isinstance(result, Exception):
            InfoBar.error("探测失败", str(result), duration=4000, parent=self.win)
            return
        self.agents = result
        self.rebuild_cards()
        self.fill_skills()
        self.fill_mcps()
        mem_pairs = [(a.name, m) for a in self.agents for m in a.memories]
        pub = agentscore.AssetFile(agent="公用大脑", path=str(core.memory_file(self.win.root)),
                                   label="共享记忆 ★ 所有已接入 agent 共读写", mtime=0)
        mem_pairs.insert(0, ("公用大脑", pub))
        self.fill_assets(self.memList, mem_pairs)
        self.fill_assets(self.cfgList, [(a.name, c) for a in self.agents for c in a.configs])
        self.fill_market()
        self.win.overview_page.set_agents(self.agents)
        InfoBar.success("探测完成", f"{len(self.agents)} 个 agent", duration=2000, parent=self.win)

    def rebuild_cards(self):
        while self.cardsRow.count():
            item = self.cardsRow.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for a in self.agents:
            card = CardWidget()
            g = QVBoxLayout(card)
            g.setContentsMargins(16, 10, 16, 10)
            g.setSpacing(2)
            g.addWidget(StrongBodyLabel(a.name))
            g.addWidget(CaptionLabel((a.home or "未检测到（可手动添加目录）")[:60]))
            g.addWidget(CaptionLabel(f"技能 {len(a.skills)} · MCP {len(a.mcps)} · "
                                     f"记忆 {len(a.memories)} · 配置 {len(a.configs)}"))
            self.cardsRow.addWidget(card)
        self.cardsRow.addStretch(1)

    def fill_skills(self):
        kw = self.skillFilter.text().strip().lower()
        self.skillList.clear()
        for a in self.agents:
            for s in a.skills:
                if kw and kw not in s.name.lower() and kw not in s.desc.lower():
                    continue
                self.skillList.addItem(f"[{a.name}]  {s.name} — {s.desc[:70]}")
                self.skillList.item(self.skillList.count() - 1).setData(Qt.UserRole, s.path)
        if not self.skillList.count():
            self.skillList.addItem("（无技能）")

    def fill_mcps(self):
        self.mcpList.clear()
        merged: dict = {}
        for a in self.agents:
            for m in a.mcps:
                if m.name in merged:
                    if a.name not in merged[m.name]["agents"]:
                        merged[m.name]["agents"].append(a.name)
                else:
                    merged[m.name] = {"agents": [a.name], "path": m.config_path}
        for name, v in sorted(merged.items()):
            self.mcpList.addItem(f"{name}   —— 配置于: {'、'.join(v['agents'])}")
            self.mcpList.item(self.mcpList.count() - 1).setData(Qt.UserRole, v["path"])
        if not self.mcpList.count():
            self.mcpList.addItem("（未探测到 MCP 配置）")

    def fill_assets(self, listw, pairs):
        listw.clear()
        for agent, asset in pairs:
            t = datetime.fromtimestamp(asset.mtime).strftime("%m-%d %H:%M") if asset.mtime else ""
            listw.addItem(f"[{agent}]  {asset.label}   {t}")
            listw.item(listw.count() - 1).setData(Qt.UserRole, asset.path)
        if not listw.count():
            listw.addItem("（无）")

    # ---- 交互
    def on_preview(self, item):
        path = item.data(Qt.UserRole)
        self.cur_path = path
        self.cur_editable = False
        self.fill_baks(path)
        if not path:
            self.preview.setHtml("<p style='color:#888'>无内容</p>")
            return
        p = Path(path)
        if self.stack.currentIndex() == 1:  # MCP：只显示来源，不读内容（防密钥外泄到界面日志）
            self.preview.setHtml(f"<p><b>{item.text()}</b></p><p>配置文件：{p}</p>")
            self.editBtn.setEnabled(False)
            self.editBtn.setText("MCP 配置含密钥，请用系统编辑器打开")
            return
        self.editBtn.setEnabled(True)
        self.editBtn.setText("编辑并保存（自动备份）")
        # 市场视图条目不可编辑
        self.cur_editable = self.stack.currentIndex() != 4 and p.is_file()
        if p.is_file():
            self.preview.setHtml(md_to_html(core.read_text(p, limit=200 * 1024)))
        else:
            self.preview.setHtml("<p style='color:#888'>(文件尚不存在，编辑保存后创建——公用记忆适合存放"
                                 "所有 agent 需要知道的环境事实、用户偏好、项目进展)</p>")
            self.cur_editable = self.stack.currentIndex() == 2

    def edit_current(self):
        if not self.cur_path or not self.cur_editable:
            return
        dlg = EditAssetDialog(self.win, self.cur_path, Path(self.cur_path).name)
        if dlg.exec():
            err = core.write_text_backed(self.cur_path, dlg.text(), root=self.win.root,
                                         agent="user",
                                         action="memory_edit" if self.stack.currentIndex() == 2 else "edit")
            if err:
                InfoBar.error("保存失败", err, duration=4000, parent=self.win)
            else:
                InfoBar.success("已保存（原文件已备份）", "", duration=2500, parent=self.win)

    def fill_baks(self, path):
        """列出当前选中资产的 agenthub 备份（仅记忆视图开放还原）。"""
        self.bakCombo.clear()
        self.bak_items = []
        if path and self.stack.currentIndex() == 2:
            p = Path(path)
            if p.parent.is_dir():
                for b in sorted(p.parent.glob(p.name + ".bak-agenthub-*"), reverse=True):
                    self.bak_items.append(b)
                    self.bakCombo.addItem(b.name)
        self.restoreBtn.setEnabled(bool(self.bak_items))

    def restore_memory(self):
        idx = self.bakCombo.currentIndex()
        if idx < 0 or idx >= len(self.bak_items) or not self.cur_path:
            return
        bak = self.bak_items[idx]
        err = core.restore_backup(self.cur_path, str(bak), root=self.win.root)
        if err:
            InfoBar.error("还原失败", err, duration=4000, parent=self.win)
        else:
            InfoBar.success("已还原（当前内容先已再备份）", bak.name, duration=3000, parent=self.win)
            self.fill_baks(self.cur_path)

    def on_open(self, item):
        path = item.data(Qt.UserRole)
        if path:
            open_location(path, select=False)

    # ---- 技能市场
    def fill_market(self):
        self.marketList.clear()
        self.market_items = agentscore.load_local_market()
        self.marketTargets.clear()
        self._market_dirs = [
            ("ZCode 技能库", Path.home() / ".agents" / "skills"),
            ("Claude 技能库", Path.home() / ".claude" / "skills"),
        ]
        for a in self.agents:
            if a.name == "hermes" and a.home:
                self._market_dirs.append(("hermes bundled-skills", Path(a.home) / "bundled-skills"))
        for label, d in self._market_dirs:
            self.marketTargets.addItem(f"安装到：{label}", str(d))
        for m in self.market_items:
            self.marketList.addItem(f"{m['name']}  ——  {m['desc'][:80]}")
            self.marketList.item(self.marketList.count() - 1).setData(
                Qt.UserRole, f"{m['repo']}|{m['identifier']}")
        if not self.market_items:
            self.marketList.addItem("（本地市场索引为空）")

    def install_selected(self):
        row = self.marketList.currentRow()
        if row < 0 or row >= len(self.market_items):
            InfoBar.warning("先在列表中选择一个技能", "", duration=2000, parent=self.win)
            return
        target = self.marketTargets.currentData()
        if not target:
            return
        m = self.market_items[row]
        InfoBar.info("开始安装", f"{m['name']} ← {m['repo']}（需网络/代理）", duration=2500, parent=self.win)
        w = FnWorker(lambda: agentscore.install_skill(m["identifier"], target), self)
        w.done.connect(self._on_installed)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _on_installed(self, result):
        text = str(result)
        if text.startswith("已安装"):
            InfoBar.success("安装完成", text, duration=4000, parent=self.win)
            self.rescan()
        else:
            InfoBar.error("安装失败", text[:120], duration=6000, parent=self.win)

    def add_agent(self):
        from PySide6.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, "选择 agent 根目录", "")
        if not d:
            return
        name = Path(d).name or "自定义agent"
        cfg = core.load_config()
        cfg.setdefault("extra_agents", {})[name] = d
        core.save_config(cfg)
        InfoBar.success("已添加", f"{name} → {d}", duration=2500, parent=self.win)
        self.rescan()


class ConnectPage(QWidget):
    """接入中心：把各 agent 连上 AgentHub 公用大脑（标准 MCP 协议）。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        top = QHBoxLayout()
        top.addWidget(SubtitleLabel("接入中心 · 公用大脑"))
        top.addStretch(1)
        refreshBtn = PushButton(ic("SYNC", "INFO"), "刷新状态")
        refreshBtn.clicked.connect(self.refresh_status)
        top.addWidget(refreshBtn)
        lay.addLayout(top)
        lay.addWidget(CaptionLabel(
            "AgentHub 通过标准 MCP 协议向所有 agent 提供 15 个工具：项目登记/新建、工作记录（可撤销）、"
            "全文搜索、团队规范、公用记忆读写、会话心跳防撞车、错误登记与查询、撤销、技能/MCP 清单、进度对齐。"
            "「一键接入」写入 MCP 配置，「注入引导」把开工规则写进 agent 的全局指令文件——"
            "两步都做，agent 才会在每次会话自然使用公用大脑，无需口头提醒。"))

        self.rows = QVBoxLayout()
        lay.addLayout(self.rows)
        lay.addWidget(StrongBodyLabel("手动接入（hermes 等未自动探测到 MCP 配置文件的 agent）"))
        self.snippet = TextBrowser()
        self.snippet.setMaximumHeight(180)
        lay.addWidget(self.snippet)
        copyBtn = PushButton(ic("COPY", "INFO"), "复制配置片段")
        copyBtn.clicked.connect(self.copy_snippet)
        lay.addWidget(copyBtn, 0, Qt.AlignRight)
        lay.addWidget(CaptionLabel("所有写入（接入/引导）均先自动备份原文件为 *.bak-agenthub-时间戳，"
                                   "可随时移除还原；操作全部进「流水」页可查。"))
        lay.addStretch(1)
        self.pyLabel = CaptionLabel("")
        lay.addWidget(self.pyLabel)
        QTimer.singleShot(400, self.refresh_status)

    def _rows_data(self):
        py, server = core.deploy_server()
        rows = []
        for t in core.MCP_TARGETS:
            data = core.read_mcp_config(t["path"])
            servers = {}
            if isinstance(data, dict):
                servers = (data.get("mcp", {}).get("servers") if t["layout"] == "zcode"
                           else data.get("mcpServers")) or {}
            rows.append({"agent": t["agent"], "path": t["path"], "layout": t["layout"],
                         "mcp": isinstance(servers, dict) and "agenthub" in servers,
                         "boot_path": t["boot"]})
        for b in core.BOOTSTRAP_ONLY:
            rows.append({"agent": b["agent"], "path": "", "layout": "", "mcp": None,
                         "boot_path": b["path"]})
        return py, server, rows

    def refresh_status(self):
        while self.rows.count():
            it = self.rows.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        py, server, rows = self._rows_data()
        if not py:
            self.pyLabel.setText("⚠ 未找到可用的 python.exe，无法自动接入（MCP server 需要 python 运行）")
        elif not server:
            self.pyLabel.setText("⚠ MCP server 部署失败")
        else:
            self.pyLabel.setText(f"服务端就绪：{server}")
        for r in rows:
            card = CardWidget()
            g = QHBoxLayout(card)
            g.setContentsMargins(18, 10, 18, 10)
            col = QVBoxLayout()
            mcp_txt = "—（用下方配置片段手动接入）" if r["mcp"] is None else \
                ("✓ 已接入公用大脑" if r["mcp"] else "○ 未接入")
            boot_on = core.bootstrap_status(r["boot_path"])
            col.addWidget(StrongBodyLabel(r["agent"]))
            col.addWidget(CaptionLabel(f"MCP：{mcp_txt}      引导规则：{'✓ 已注入' if boot_on else '○ 未注入'}"))
            if r["path"]:
                col.addWidget(CaptionLabel(f"配置文件：{r['path']}"))
            col.addWidget(CaptionLabel(f"引导写入：{r['boot_path']}"))
            g.addLayout(col, 1)
            if r["mcp"] is not None:
                if r["mcp"]:
                    rm = PushButton(ic("CLOSE", "INFO"), "移除接入")
                    rm.clicked.connect(lambda _, p=r["path"], l=r["layout"]: self.do_remove(p, l))
                    g.addWidget(rm)
                else:
                    btn = PrimaryPushButton(ic("LINK", "INFO"), "一键接入")
                    btn.setEnabled(bool(py and server))
                    btn.clicked.connect(lambda _, p=r["path"], l=r["layout"]: self.do_connect(p, l))
                    g.addWidget(btn)
            if boot_on:
                rb = PushButton("移除引导")
                rb.clicked.connect(lambda _, p=r["boot_path"]: self.do_boot_remove(p))
                g.addWidget(rb)
            else:
                ib = PrimaryPushButton(ic("EDIT", "INFO"), "注入引导")
                ib.clicked.connect(lambda _, p=r["boot_path"]: self.do_boot_inject(p))
                g.addWidget(ib)
            self.rows.addWidget(card)

    def do_connect(self, path, layout):
        py, server, _ = self._rows_data()
        if not py or not server:
            InfoBar.error("无法接入", "python 或服务端未就绪", duration=3000, parent=self.win)
            return
        err = core.install_mcp_entry(path, layout, py, server, self.win.root)
        if err:
            InfoBar.error("接入失败", err, duration=5000, parent=self.win)
        else:
            core.journal(self.win.root, "user", "mcp_connect", path)
            InfoBar.success("已接入", f"{path}（原文件已备份）", duration=3000, parent=self.win)
        self.refresh_status()

    def do_remove(self, path, layout):
        err = core.remove_mcp_entry(path, layout)
        if not err:
            core.journal(self.win.root, "user", "mcp_remove", path)
        InfoBar.success("已移除" if not err else "移除失败", err, duration=3000, parent=self.win)
        self.refresh_status()

    def do_boot_inject(self, path):
        err = core.inject_bootstrap(path, self.win.root)
        if err:
            InfoBar.error("注入失败", err, duration=5000, parent=self.win)
        else:
            InfoBar.success("引导已注入", f"{path}（原文件已备份，重启该 agent 生效）",
                            duration=3000, parent=self.win)
        self.refresh_status()

    def do_boot_remove(self, path):
        err = core.remove_bootstrap(path, self.win.root)
        InfoBar.success("引导已移除" if not err else "移除失败", err, duration=3000, parent=self.win)
        self.refresh_status()

    def _snippet_text(self):
        py, server, _ = self._rows_data()
        entry = {"agenthub": {"command": py or "D:/python311/python.exe",
                              "args": [server or r"C:\\Users\\y\\.agenthub\\mcp_server\\agenthub_mcp.py",
                                       self.win.root or "D:/AgentHub"]}}
        return json.dumps({"mcpServers": entry}, ensure_ascii=False, indent=2)

    def copy_snippet(self):
        QApplication.clipboard().setText(self._snippet_text())
        InfoBar.success("已复制", "粘贴到 agent 的 MCP 配置里即可", duration=2500, parent=self.win)


class HelpPage(QWidget):
    """软件内帮助：有什么不懂的在这里找。"""

    CONTENT = """
    <h2>AgentHub 是什么</h2>
    <p>你电脑上所有 AI agent 的<b>公用大脑</b>和统一工作台：项目记录、投入产出文件、
    工作记录、技能、MCP、记忆，全部在一个地方；agent 通过 MCP 协议接入后共享同一个记忆和进度，
    错误可查、操作可撤销、同项目干活有撞车预警。</p>
    <h2>各页面怎么用</h2>
    <ul>
    <li><b>总览</b>：今天谁在干活、待处理数量、agent 阵容与当前活跃会话，一眼全局。</li>
    <li><b>项目</b>：左侧列表（最近活动优先），右侧看工作记录全文和文件总览；新建项目强制「对象-问题」命名；从 00_Inbox 分拣投入文件。</li>
    <li><b>时间线</b>：所有 agent 的工作记录按时间倒序，可按 agent/项目过滤，点击条目看完整操作步骤。</li>
    <li><b>流水</b>：错误登记（agent 出错自动/主动上报，可标记已修、看回滚方式）+ 操作流水（每次写操作一条，记录类可一键撤销）。</li>
    <li><b>统计</b>：项目数、记录数、各 agent 工作量、月度活跃。</li>
    <li><b>搜索</b>：全文+文件名搜索，Ctrl+F 直达。</li>
    <li><b>能力中心</b>：各 agent 的技能库 / MCP / 记忆 / 全局配置聚合；「技能市场」可从 anthropics/skills 安装新技能；记忆支持软件内编辑与历史备份还原（自动备份）。</li>
    <li><b>接入</b>：一键写 MCP 配置 + 注入开工引导到 agent 全局指令文件（均自动备份、可移除）；hermes 等复制配置片段手动粘贴。</li>
    <li><b>对账</b>：揪出 agent 前缀平行目录、重复项目、野目录，杜绝记录分裂。</li>
    </ul>
    <h2>agent 怎么接入公用大脑（两步）</h2>
    <p>1. 到「接入」页对某个 agent 点「一键接入」+「注入引导」（ZCode / Claude Code / Codex / DSH 支持）；<br>
    2. 重启对应 agent——它每次开工就会自动读进度和记忆、干完活自动写记录、出错自动登记，无需口头提醒。</p>
    <h2>公用记忆是什么</h2>
    <p>能力中心 → 记忆 → 第一条「共享记忆」。所有已接入的 agent 都能读写（MCP 工具 hub_memory_read / hub_memory_write）。
    适合存放：机器环境事实、你的偏好、跨 agent 的项目进展。任何 agent 学到的东西，其他 agent 下次开工先读它。</p>
    <h2>常见问题</h2>
    <ul>
    <li><b>数据在哪？</b>根目录（默认桌面 zcode项目记录）就是唯一真理，软件随时可删可重装；_hub 下是共享记忆/规则/流水/错误登记。</li>
    <li><b>会改我的 agent 配置吗？</b>只有你在接入页主动点「接入/注入引导」才会写，且每次写前自动备份、可一键移除、操作进流水。</li>
    <li><b>MCP 配置里的密钥会被展示吗？</b>不会，界面只显示服务器名称和来源。</li>
    <li><b>agent 记错了怎么撤？</b>「流水」页选中该条记录点撤销（只撤各 agent 最新一条，原文先备份）。</li>
    <li><b>两个 agent 撞车怎么办？</b>agent 开工调 hub_heartbeat，同项目有别人活跃时会收到预警；总览页也能看到谁在干活。</li>
    </ul>
    """

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.addWidget(SubtitleLabel("帮助"))
        browser = TextBrowser()
        browser.setHtml(self.CONTENT)
        browser.setOpenExternalLinks(False)
        lay.addWidget(browser, 1)


class SettingsPage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)

        c1 = CardWidget()
        g1 = QVBoxLayout(c1)
        g1.setContentsMargins(20, 14, 20, 14)
        g1.addWidget(StrongBodyLabel("根目录"))
        row = QHBoxLayout()
        self.rootLabel = BodyLabel("未设置")
        pickBtn = PushButton(ic("FOLDER_ADD", "INFO"), "选择…")
        self.initBtn = PrimaryPushButton(ic("EMBED", "INFO"), "初始化目录结构")
        row.addWidget(self.rootLabel, 1)
        row.addWidget(pickBtn)
        row.addWidget(self.initBtn)
        g1.addLayout(row)
        g1.addWidget(CaptionLabel("可以直接选现有目录（如桌面 zcode项目记录）立即可视化历史；"
                                 "也可以选新目录（推荐 D:\\AgentHub）后初始化全新结构。软件纯本地扫描，不改任何 agent。"))
        lay.addWidget(c1)

        c1b = CardWidget()
        g1b = QVBoxLayout(c1b)
        g1b.setContentsMargins(20, 14, 20, 14)
        g1b.addWidget(StrongBodyLabel("窗口大小（点一下立即生效，边缘拖拽外的一键兜底）"))
        sizeRow = QHBoxLayout()
        for label, wd, ht in (("紧凑 1180×760", 1180, 760), ("标准 1400×900", 1400, 900),
                              ("宽敞 1616×950", 1616, 950)):
            b = PushButton(label)
            b.clicked.connect(lambda _, w_=wd, h_=ht: self.win.showNormal() or self.win.resize(w_, h_))
            sizeRow.addWidget(b)
        sizeRow.addStretch(1)
        g1b.addLayout(sizeRow)
        g1b.addWidget(CaptionLabel("也可以用键盘：Win+←/→ 贴靠半屏，Alt+空格→大小 用方向键精调。"))
        lay.addWidget(c1b)

        c2 = CardWidget()
        g2 = QVBoxLayout(c2)
        g2.setContentsMargins(20, 14, 20, 14)
        g2.addWidget(StrongBodyLabel("规则（存 _hub\\rules.md，将来 agent 接入后由工具注入）"))
        self.rulesEdit = TextEdit()
        self.rulesEdit.setMinimumHeight(220)
        g2.addWidget(self.rulesEdit)
        saveBtn = PushButton(ic("SAVE", "INFO"), "保存规则")
        g2.addWidget(saveBtn, 0, Qt.AlignRight)
        lay.addWidget(c2)
        lay.addStretch(1)

        pickBtn.clicked.connect(self.pick_root)
        self.initBtn.clicked.connect(self.init_struct)
        saveBtn.clicked.connect(self.save_rules)

    def showEvent(self, e):
        self.reload()
        super().showEvent(e)

    def reload(self):
        self.rootLabel.setText(self.win.root or "未设置")
        if self.win.root:
            self.rulesEdit.setPlainText(core.load_rules(self.win.root))

    def pick_root(self):
        from PySide6.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, "选择根目录", self.win.root or "")
        if d:
            self.win.set_root(d)

    def init_struct(self):
        if not self.win.root:
            InfoBar.warning("先选择根目录", "", duration=2000, parent=self.win)
            return
        err = core.init_hub(self.win.root)
        if err:
            InfoBar.error("初始化失败", err, duration=4000, parent=self.win)
            return
        InfoBar.success("已初始化", "00_Inbox / 99_Archive / _hub（规则·共享记忆·操作流水·错误登记）",
                        duration=3000, parent=self.win)
        self.win.refresh()

    def save_rules(self):
        if not self.win.root:
            return
        err = core.save_rules(self.win.root, self.rulesEdit.toPlainText())
        if err:
            InfoBar.error("保存失败", err, duration=3000, parent=self.win)
        else:
            InfoBar.success("已保存", "", duration=1500, parent=self.win)


# ---------------------------------------------------------------- 主窗口

class AgentHubWindow(FluentWindow):
    """主窗口。边缘缩放：原生 hit-test（BORDER_WIDTH 加宽）+ Qt QSizeGrip 角落手柄双保险。"""

    def __init__(self):
        super().__init__()
        self.root = core.get_root()

        self.overview_page = OverviewPage(self)
        self.project_page = ProjectPage(self)
        self.timeline_page = TimelinePage(self)
        self.stats_page = StatsPage(self)
        self.search_page = SearchPage(self)
        self.audit_page = AuditPage(self)
        self.journal_page = JournalPage(self)
        self.hub_page = HubPage(self)
        self.connect_page = ConnectPage(self)
        self.help_page = HelpPage(self)
        self.settings_page = SettingsPage(self)

        for w, icon, text in (
            (self.overview_page, "HOME", "总览"),
            (self.project_page, "FOLDER", "项目"),
            (self.timeline_page, "HISTORY", "时间线"),
            (self.journal_page, "DICTIONARY", "流水"),
            (self.stats_page, "TILES", "统计"),
            (self.search_page, "SEARCH", "搜索"),
            (self.hub_page, "LIBRARY", "能力中心"),
            (self.connect_page, "LINK", "接入"),
            (self.audit_page, "FILTER", "对账"),
            (self.help_page, "INFO", "帮助"),
            (self.settings_page, "SETTING", "设置"),
        ):
            w.setObjectName(text)
            self.addSubInterface(w, ic(icon), text)

        self._narrow_nav()
        # 边缘拖拽：qframelesswindow 原生链命中带默认 5 物理像素（高分屏拖不到），加宽兜底；
        # 另加 Qt QSizeGrip 角落手柄（startSystemResize，不依赖原生 hit-test）
        self.setResizeEnabled(True)
        self.BORDER_WIDTH = 14
        self.setMinimumSize(920, 600)
        self._grips = [QSizeGrip(self), QSizeGrip(self)]
        for g in self._grips:
            g.setFixedSize(24, 24)
            g.setStyleSheet("background: transparent;")
            g.raise_()
        self.setWindowTitle("AgentHub")
        self.resize(1180, 760)
        geo = core.load_config().get("win_geometry", "")
        if geo:
            from PySide6.QtCore import QByteArray
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        f5 = QShortcut(QKeySequence("F5"), self)
        f5.activated.connect(self.refresh)
        cf = QShortcut(QKeySequence("Ctrl+F"), self)
        cf.activated.connect(self.goto_search)
        self._workers = []  # 持住运行中的 QThread 引用，防 GC 销毁运行中线程导致进程崩溃
        QTimer.singleShot(200, self.refresh)
        if not self.root:
            self.switchTo(self.settings_page)

    def resizeEvent(self, e):
        if getattr(self, "_grips", None):
            self._grips[0].move(self.width() - 24, self.height() - 24)  # 右下
            self._grips[1].move(0, self.height() - 24)                  # 左下
        super().resizeEvent(e)

    def _narrow_nav(self):
        """导航栏按上次退出时的显示模式恢复（qfw 有 COMPACT/MENU/EXPAND 三态，
        用户点过汉堡展开后即使悬浮展开也应恢复为展开）。"""
        nav = self.navigationInterface
        try:
            nav.setCollapsible(True)
        except Exception:
            pass
        try:
            nav.setExpandWidth(96)
        except Exception:
            pass
        panel = getattr(nav, "panel", None)
        if panel is None or not hasattr(panel, "collapse"):
            return
        if core.load_config().get("nav_collapsed", True):
            QTimer.singleShot(0, panel.collapse)  # 窗口显示后执行，初始化前调用无效
        else:
            QTimer.singleShot(0, panel.expand)

    def _nav_collapsed_now(self):
        """用户视角的收起状态：仅 COMPACT 算收起；MENU（悬浮展开）/EXPAND 都算展开。"""
        panel = getattr(self.navigationInterface, "panel", None)
        mode = getattr(panel, "displayMode", None)
        return getattr(mode, "name", "COMPACT") == "COMPACT"

    def closeEvent(self, e):
        """退出前：等后台线程结束（防 QThread 运行中被销毁的偶发报错），再记住状态。"""
        for lst in (self._workers, getattr(self.hub_page, "_workers", []),
                    getattr(self.search_page, "_workers", [])):
            for t in list(lst):
                try:
                    t.wait(2000)
                except Exception:
                    pass
        try:
            cfg = core.load_config()
            cfg["win_geometry"] = bytes(self.saveGeometry().toBase64()).decode()
            cfg["nav_collapsed"] = self._nav_collapsed_now()
            core.save_config(cfg)
        except Exception:
            pass
        super().closeEvent(e)

    def goto_search(self):
        self.switchTo(self.search_page)
        self.search_page.edit.setFocus()

    def set_root(self, d):
        self.root = d
        core.set_root(d)
        self.settings_page.reload()
        self.refresh()

    def refresh(self):
        if not self.root:
            return
        w = ScanWorker(self.root, self)
        w.done.connect(self.apply)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def apply(self, snap):
        if snap.error and not snap.projects:
            InfoBar.error("扫描失败", snap.error, duration=4000, parent=self)
        if self.root and not (Path(self.root) / core.DIR_META).is_dir() \
                and not getattr(self, "_hub_warned", False):
            self._hub_warned = True
            InfoBar.warning("公用大脑未初始化",
                            "到「设置」页点「初始化目录结构」——启用共享记忆、操作流水、错误登记",
                            duration=6000, parent=self)
        self.project_page.set_snapshot(snap)
        self.timeline_page.set_snapshot(snap)
        self.stats_page.set_snapshot(snap)
        self.audit_page.set_snapshot(snap)
        self.overview_page.set_snapshot(snap)
        self.overview_page.set_sessions(core.active_sessions(self.root) if self.root else [])
        self.journal_page.reload()


def run_gui():
    setTheme(Theme.AUTO)
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 9))
    w = AgentHubWindow()
    w.show()
    rc = app.exec()
    os._exit(rc)  # qfluentwidgets item view 退出挂起兜底（已知坑）


if __name__ == "__main__":
    run_gui()
