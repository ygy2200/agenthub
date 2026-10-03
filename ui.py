# -*- coding: utf-8 -*-
"""AgentHub GUI：总览 / 项目 / 时间线 / Agent 中心 / 大脑 / 流水·对账 / 统计 / 搜索 / 接入。

数据全部来自 core.scan 现场扫描与大脑数据库（brain.db），agent 直写后按 F5 即见。
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
from PySide6.QtGui import QShortcut, QKeySequence, QFont, QCursor, QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QVBoxLayout, QWidget, QHeaderView, QAbstractItemView,
                               QStackedWidget, QSizeGrip, QSplitter, QTextEdit)
from qfluentwidgets import (BodyLabel, CaptionLabel, CardWidget, ComboBox, FluentIcon as FIF,
                            FluentWindow, InfoBar, LineEdit, ListWidget, MessageBoxBase,
                            NavigationItemPosition, PrimaryPushButton, ProgressBar, PushButton,
                            ScrollArea, SearchLineEdit, SegmentedWidget, StrongBodyLabel,
                            SubtitleLabel, TextBrowser, TextEdit, TitleLabel, setTheme, Theme)

import agentscore
import brain
import core


def ic(name, fallback="INFO"):
    """图标枚举兜底，防不同版本枚举名缺失导致崩。"""
    return getattr(FIF, name, getattr(FIF, fallback))


AGENT_BADGE_COLOR = {
    "zcode": "#0078d4", "hermes": "#8764b8", "deepseek": "#0a7ea4",
    "claude": "#c76a42", "codex": "#4a6b8a", "dsh": "#00a67e", "其他": "#666666",
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
    done = Signal(object)

    def __init__(self, root, keyword, parent=None):
        super().__init__(parent)
        self.root, self.keyword = root, keyword

    def run(self):
        try:
            self.done.emit(brain.search_all(self.root, self.keyword))
        except Exception as e:  # noqa: BLE001
            self.done.emit(e)


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
    """时间线记录详情：标题 + 元信息 + 段正文渲染（接收 DB records 行 dict）。"""

    def __init__(self, win, r: dict):
        super().__init__(win)
        self.titleLabel = SubtitleLabel((r.get("title") or "(无标题)")[:60])
        meta = QHBoxLayout()
        meta.addWidget(badge(r.get("agent", ""), ""))
        meta.addWidget(CaptionLabel(f"{r.get('date') or '日期未标注'} · {r.get('project', '')} · 记录#{r.get('id', '?')}"))
        meta.addStretch(1)
        self.browser = TextBrowser()
        self.browser.setHtml(md_to_html(r.get("content") or r.get("title") or ""))
        self.browser.setMinimumSize(860, 480)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addLayout(meta)
        self.viewLayout.addWidget(self.browser)
        openBtn = PushButton(ic("FOLDER", "INFO"), "打开项目目录")
        openBtn.clicked.connect(lambda: open_location(str(Path(win.root) / r.get("project", "")), select=False))
        self.viewLayout.addWidget(openBtn, 0, Qt.AlignRight)
        self.yesButton.setText("关闭")
        self.cancelButton.hide()
        self.widget.setMinimumWidth(920)


# ---------------------------------------------------------------- 小组件

_BARE_TITLE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}（[^）]*）$")


def _display_title(r: dict) -> str:
    """空洞标题（仅"日期（agent）"，MCP 旧写入与迁移记录皆如此）用 content 首行
    （目的行优先）替代显示——title 原值是迁移幂等键，只改显示不动库。"""
    t = (r.get("title") or "").strip()
    if _BARE_TITLE_RE.match(t) and r.get("content"):
        m = re.search(r"目的[】\]:：]\s*(.+)", r["content"])
        first = (m.group(1) if m else
                 next((ln.strip() for ln in r["content"].splitlines() if ln.strip()), ""))
        first = first.strip("【】 ").strip()
        if first:
            return f"{t} · {first[:56]}{'…' if len(first) > 56 else ''}"
    return t or "(无标题)"


KIND_BADGE_COLOR = {
    "lesson": "#c76a42", "fact": "#0078d4", "preference": "#8764b8",
    "project": "#00a67e", "note": "#666666",
}


def kind_badge(kind: str) -> QLabel:
    """记忆类型徽章：唤起排行/置顶记忆的正文行用小字灰文本看不清，类型先入彩色徽章分层。"""
    lb = QLabel(brain.KIND_CN.get(kind, kind))
    lb.setStyleSheet(
        f"color:white;background:{KIND_BADGE_COLOR.get(kind, '#8a8a8a')};"
        "border-radius:8px;padding:1px 8px;font-size:11px;")
    lb.setFixedHeight(20)
    return lb


def agent_disp(agent: str) -> str:
    """流水/待办行里的 agent 显示名：空/unknown（历史数据来源）统一显示"未标注"。"""
    return "未标注" if (agent or "").strip().lower() in ("", "unknown") else agent


def badge(agent: str, raw: str) -> QLabel:
    # 未知 agent 显示自己的名字（中性色），"未标注"仅限 agent 字段真空——
    # 否则新接入的 agent 一律显示"未标注"（2026-10-01 dsh 实测踩坑）
    text = {"zcode": "ZCode", "hermes": "hermes", "deepseek": "deepseek",
            "claude": "claude", "codex": "codex", "dsh": "DSH"}.get(agent, raw or agent or "未标注")
    lb = QLabel(text)
    lb.setStyleSheet(
        f"color:white;background:{AGENT_BADGE_COLOR.get(agent, '#8a8a8a')};"
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


class MemoryEditDialog(MessageBoxBase):
    """大脑记忆编辑器：内容 + 类型 + 标签 + 置顶（编辑已有条目传 m，新建传 None）。"""

    def __init__(self, win, m: dict | None):
        super().__init__(win)
        self.titleLabel = SubtitleLabel("编辑记忆" if m else "新建记忆")
        self.edit = TextEdit()
        self.edit.setPlainText((m or {}).get("content", ""))
        self.edit.setMinimumSize(720, 360)
        row = QHBoxLayout()
        row.addWidget(CaptionLabel("类型"))
        self.kindCombo = ComboBox()
        for k, cn in brain.KIND_CN.items():
            self.kindCombo.addItem(f"{cn}（{k}）", k)
        if m:
            idx = self.kindCombo.findData(m.get("kind", "note"))
            self.kindCombo.setCurrentIndex(idx if idx >= 0 else 4)
        else:
            self.kindCombo.setCurrentIndex(4)  # 默认 note
        row.addWidget(self.kindCombo)
        row.addWidget(CaptionLabel("标签（逗号分隔）"))
        self.tagsEdit = LineEdit()
        self.tagsEdit.setText((m or {}).get("tags", ""))
        row.addWidget(self.tagsEdit, 1)
        self.pinBox = None
        from qfluentwidgets import CheckBox
        self.pinBox = CheckBox("置顶（优先展示）")
        self.pinBox.setChecked(bool((m or {}).get("pinned", False)))
        row.addWidget(self.pinBox)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.edit)
        self.viewLayout.addLayout(row)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(780)

    def content(self):
        return self.edit.toPlainText()

    def kind(self):
        return self.kindCombo.currentData() or "note"

    def tags(self):
        return self.tagsEdit.text().strip()

    def pinned(self):
        return bool(self.pinBox and self.pinBox.isChecked())


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
        file_col.addWidget(self.fileList, 1)
        self.fileList.itemDoubleClicked.connect(self.open_file_loc)

        self.nameLabel = TitleLabel("—")
        right.addWidget(self.nameLabel)
        right.addLayout(head)
        right.addWidget(self.pathLabel)
        right.addWidget(self.issueLabel)
        right.addWidget(self.browser, 1)
        right.addLayout(file_col, 1)  # 文件总览也参与伸缩：窗口矮时说明行不再被裁出可视区

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
        try:
            self.db_projects = brain.list_projects(self.win.root, 200)
        except Exception:
            self.db_projects = []
        selected = self.current
        names = [p["name"] for p in self.db_projects]
        self.rebuild_list()
        if selected in names:
            self.listw.setCurrentRow(names.index(selected))
        elif self.listw.count():
            self.listw.setCurrentRow(0)

    def rebuild_list(self):
        kw = self.filterEdit.text().strip().lower()
        self.listw.blockSignals(True)
        self.listw.clear()
        for p in getattr(self, "db_projects", []):
            if kw in p["name"].lower():
                n = p.get("n_records", 0)
                self.listw.addItem(f"{p['name']}   ({n}条)")
        self.listw.blockSignals(False)
        if self.listw.count():
            self.listw.setCurrentRow(0)

    def on_select(self, row):
        if row < 0 or row >= self.listw.count():
            return
        name = self.listw.item(row).text().rsplit("   (", 1)[0]
        proj_scan = next((p for p in self.snap.projects if p.name == name), None) if self.snap else None
        self.current = name
        self.nameLabel.setText(name)
        self.pathLabel.setText(str(Path(self.win.root) / name))
        issues = proj_scan.issues if proj_scan else []
        self.issueLabel.setText("⚠ " + "；".join(issues) if issues else "")
        # 工作记录正文：大脑数据库
        try:
            recs = brain.list_records(self.win.root, project=name, limit=500)
        except Exception:
            recs = []
        if recs:
            md = "\n\n".join(f"## {r['title']}\n{r['content']}" for r in reversed(recs))
            self.browser.setHtml(md_to_html(md))
        elif proj_scan and proj_scan.doc_path:
            self.browser.setHtml(md_to_html(core.read_text(Path(proj_scan.doc_path))))
        else:
            self.browser.setHtml("<p style='color:#888'>大脑中无此项目记录，目录中也无可用主文档</p>")
        self.fileList.clear()
        if proj_scan:
            for group, rp, fp in proj_scan.files:
                self.fileList.addItem(f"[{group}]  {rp}")
                self.fileList.item(self.fileList.count() - 1).setData(Qt.UserRole, fp)
            if not proj_scan.files:
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
            "claude": "claude", "codex": "codex", "dsh": "DSH", "其他": "其他"}


class TimelinePage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.all_entries: list = []
        self.projects: list = []
        self.render_limit = 80  # 懒加载：每批渲染条数
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

    def set_records(self, rows: list):
        """时间线数据源 = 大脑数据库 records 表（dict 行）。"""
        self.projects = sorted({r["project"] for r in rows})
        entries = rows
        self.all_entries = entries

        # 过滤器填充（保持当前选择）；agent 候选统一小写去重（大小写双身份曾致过滤滤光记录）；
        # 项目列表取全量（无记录项目也可过滤）
        agents = ["全部"] + [AGENT_CN.get(a, a) for a in
                             sorted({(r.get("agent") or "").lower() for r in rows} - {""})]
        try:
            all_projects = [p["name"] for p in brain.list_projects(self.win.root, 200)]
        except Exception:
            all_projects = self.projects
        projs = ["全部"] + all_projects
        for combo, items in ((self.agentFilter, agents), (self.projFilter, projs)):
            combo.blockSignals(True)
            cur = combo.currentText()
            combo.clear()
            combo.addItems(items)
            idx = combo.findText(cur)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)
        self.rebuild()  # 新快照：全清重渲（rendered/_last_date 重置）

    def _filtered(self):
        a = self.agentFilter.currentText()
        pj = self.projFilter.currentText()
        out = self.all_entries
        if a and a != "全部":
            # casefold 比较：agent 存值大小写曾混杂（ZCode/zcode），精确 == 会滤光记录
            out = [r for r in out if (r.get("agent") or "").lower() == a.lower()]
        if pj and pj != "全部":
            out = [r for r in out if r.get("project", "") == pj]
        return out

    def _clear_layout(self, lay):
        """递归清理子布局：只 deleteLater widget 项会让装在子布局里的按钮泄漏残留
        （2026-10-01 时间线"加载更多"按钮堆叠 bug 根因）。"""
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                self._clear_layout(it.layout())
                it.layout().deleteLater()

    def _clear_box(self):
        while self.box.count():
            item = self.box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())
                item.layout().deleteLater()

    def rebuild(self):
        self._clear_box()
        self.rendered = 0
        self._last_date = None
        self._cur_card_lay = None
        self._append_batch()

    def _append_batch(self):
        """增量渲染下一批（load_more 不整页重建：不闪屏、不丢滚动位置）。
        _cur_card_lay 跨批次保存最后一张日期卡——批次首条与上批末条同日时继续入同卡。"""
        entries = self._filtered()
        batch = entries[self.rendered:self.rendered + self.render_limit]
        for r in batch:
            d = r.get("date") or "日期未标注"
            if d != self._last_date or self._cur_card_lay is None:
                self._last_date = d
                card = CardWidget()
                self._cur_card_lay = QVBoxLayout(card)
                self._cur_card_lay.setContentsMargins(18, 12, 18, 12)
                self._cur_card_lay.setSpacing(6)
                self._cur_card_lay.addWidget(StrongBodyLabel(d))
                self.box.addWidget(card)
            row = QHBoxLayout()
            row.addWidget(badge(r.get("agent", ""), ""))
            title = ClickBodyLabel(_display_title(r)[:80])
            title.setToolTip(f"{r.get('project', '')} · {_display_title(r)}\n点击查看完整记录")
            title.setCursor(Qt.PointingHandCursor)
            title.clicked.connect(lambda _, rr=r: self.show_detail(rr))
            proj_btn = PushButton(r.get("project", "")[:10] + ("…" if len(r.get("project", "")) > 10 else ""))
            proj_btn.setFixedHeight(26)
            proj_btn.setMaximumWidth(140)  # 长项目名不设上限会把行撑出可视区，按钮被裁成半个
            proj_btn.setToolTip(r.get("project", ""))
            proj_btn.clicked.connect(lambda _, n=r.get("project", ""): self.jump(n))
            row.addWidget(title, 1)
            row.addWidget(proj_btn)
            self._cur_card_lay.addLayout(row)
        self.rendered += len(batch)
        if not entries:
            tip = BodyLabel("暂无符合条件的记录")
            tip.setAlignment(Qt.AlignCenter)
            self.box.addWidget(tip)
            return
        # 尾部：加载更多按钮 + 统计说明（直接放 box，居中对齐，增量时先移除旧尾部）
        for tw in getattr(self, "_tail", []):
            tw.setParent(None)
            tw.deleteLater()
        self._tail = []
        if self.rendered < len(entries):
            more = PushButton(f"加载更多（还有 {len(entries) - self.rendered} 条）")
            more.clicked.connect(self.load_more)
            self.box.addWidget(more, 0, Qt.AlignHCenter)
            self._tail.append(more)
            cap = CaptionLabel(f"共 {len(entries)} 条，已显示 {self.rendered} 条；用上方过滤器可缩小范围")
            self.box.addWidget(cap, 0, Qt.AlignHCenter)
            self._tail.append(cap)

    def load_more(self):
        self._append_batch()

    def show_detail(self, r):
        dlg = RecordDetailDialog(self.win, r)
        dlg.exec()

    def jump(self, name):
        self.win.project_page.select_project(name)
        self.win.switchTo(self.win.project_page)


class BrainPage(QWidget):
    """大脑页（v2.7）：唤起排行 / 使用分布 / 待办看板 / 置顶记忆——
    把"写多读少"变得可见：use_count、推送与检索计数直接对应大脑验收四条标准。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        # 内容装入滚动区：唤起/置顶/待办行数随真实数据增长，默认窗口高度装不下时
        # QVBoxLayout 会把每行强制压扁成重叠细条（"看不清"的最终根因），滚动化后永不压缩
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.enableTransparentBackground()
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 24, 24, 24)
        row = QHBoxLayout()
        self.cardMem = NumberCard("记忆总数")
        self.cardCrys = NumberCard("结晶率（记忆/记录）")
        self.cardPush = NumberCard("记忆推送累计")
        self.cardTodo = NumberCard("待办线索")
        for c in (self.cardMem, self.cardCrys, self.cardPush, self.cardTodo):
            row.addWidget(c)
        lay.addLayout(row)

        self.topBox = QVBoxLayout()
        self.useBox = QVBoxLayout()
        self.todoBox = QVBoxLayout()
        self.pinBox = QVBoxLayout()
        for box, t in ((self.topBox, "记忆唤起 top10（开工推送反射弧记数）"),
                       (self.useBox, "大脑使用分布（主动检索 + 推送接收，按 agent）"),
                       (self.todoBox, "待办看板（记录里提取的欠账，处理后勾销闭环）"),
                       (self.pinBox, "置顶记忆（置顶位只放工作知识）")):
            box.setSpacing(10)  # 行距：无间距时唤起/置顶行挤成一片
            gb = CardWidget()
            g = QVBoxLayout(gb)
            g.setContentsMargins(20, 14, 20, 14)
            g.addWidget(StrongBodyLabel(t))
            g.addLayout(box)
            lay.addWidget(gb)
        lay.addStretch(1)
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll, 1)

    def reload(self):
        root = self.win.root
        if not root:
            return
        try:
            h = brain.health_report(root)
        except Exception:
            return
        # 滚动区内容尺寸可能随数据变化，刷新后回到顶部避免停在旧位置
        self.scroll.verticalScrollBar().setValue(0)
        self.cardMem.value.setText(str(h["memories"]))
        self.cardCrys.value.setText(f"{h['crystallization']}%")
        self.cardPush.value.setText(str(h.get("recall_push_total", 0)))
        self.cardTodo.value.setText(str(h["todo_count"]))

        def clear(box):
            while box.count():
                it = box.takeAt(0)
                wid = it.widget()
                if wid:  # 先摘出父级再排队销毁：立即从界面消失，不与新一轮内容重叠
                    wid.setParent(None)
                    wid.deleteLater()
                sub = it.layout()
                if sub:  # 行布局（徽章+进度条/文本+按钮）也是布局项，不递归拆净子控件会残留叠加
                    clear(sub)

        # 唤起排行（验收①：推送 top 应是工作知识而非元信息）
        clear(self.topBox)
        tops = h.get("top_pushed", [])
        for m in tops:
            self.topBox.addWidget(self._mem_row(f"#{m['id']} ×{m['use_count']}", m))
        if not tops:
            self.topBox.addWidget(CaptionLabel(
                "还没有记忆被唤起——agent 开工心跳（hub_heartbeat）时自动推送相关记忆，此处累计记数"))

        # 使用分布（验收①：searches 有跨 agent 真实数据）
        clear(self.useBox)
        merged: dict = {}
        for src in (h.get("search_by_agent", {}), h.get("push_by_agent", {})):
            for k, v in src.items():
                merged[k] = merged.get(k, 0) + v
        total = max(1, sum(merged.values()))
        for k, v in sorted(merged.items(), key=lambda x: -x[1]):
            r = QHBoxLayout()
            r.addWidget(badge(k, k))
            bar = ProgressBar()
            bar.setValue(round(v / total * 100))
            r.addWidget(bar, 1)
            r.addWidget(BodyLabel(str(v)))
            self.useBox.addLayout(r)
        if not merged:
            self.useBox.addWidget(CaptionLabel("暂无检索/推送记录"))

        # 待办看板（验收③：欠账可见 + 勾销闭环）
        clear(self.todoBox)
        for t in h["todos"][:8]:
            r = QHBoxLayout()
            r.addWidget(BodyLabel(f"[{t['project']}·{t['date'][5:]}·{t['agent']}] {t['todo'][:56]}"), 1)
            btn = PushButton("勾销")
            btn.clicked.connect(lambda _, rid=t["id"], tt=t["todo"]: self._done_todo(rid, tt))
            r.addWidget(btn)
            self.todoBox.addLayout(r)
        if not h["todos"]:
            self.todoBox.addWidget(CaptionLabel("暂无待办（从记录提取：后续/待验证/下一步…句式）"))

        # 置顶记忆（验收④：置顶位工作知识占比）
        clear(self.pinBox)
        try:
            pins = [m for m in brain.search_memories(root, "", "", 100) if m["pinned"]][:8]
        except Exception:
            pins = []
        for m in pins:
            self.pinBox.addWidget(self._mem_row(f"★ #{m['id']}", m))
        if not pins:
            self.pinBox.addWidget(CaptionLabel("暂无置顶——agent 写记忆时可带 pinned=true 进置顶位"))

    @staticmethod
    def _mem_row(head: str, m: dict) -> QWidget:
        """唤起/置顶行：类型徽章 + 编号·次数 + 正文（正常字号可换行）。
        原来是单行 CaptionLabel 灰小字且 [:66]/[:70] 截断，是"看不清"的另一半根因。"""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.addWidget(kind_badge(m["kind"]))
        body = BodyLabel(f"{head}  {m['content'][:100]}")
        body.setWordWrap(True)
        h.addWidget(body, 1)
        return w

    def _done_todo(self, rid, todo):
        err = brain.mark_todo_done(self.win.root, rid, todo, agent="gui")
        if err:
            InfoBar.error("勾销失败", err, duration=4000, parent=self.win)
            return
        InfoBar.success("已勾销", "该待办不再出现（闭环），操作可在流水页追溯", duration=3000, parent=self.win)
        self.reload()


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
        self.cardStalled = NumberCard("停滞项目（90天）")
        self.cardSearch = NumberCard("检索（今日/累计）")
        for c in (self.cardProj, self.cardRec, self.cardIssue, self.cardStalled, self.cardSearch):
            row.addWidget(c)
        lay.addLayout(row)

        self.agentBox = QVBoxLayout()
        self.monthBox = QVBoxLayout()
        self.healthLine = CaptionLabel("")
        self.healthLine.setWordWrap(True)
        for box, t in ((self.agentBox, "各 agent 记录分布"), (self.monthBox, "近 6 个月活跃")):
            gb = CardWidget()
            g = QVBoxLayout(gb)
            g.setContentsMargins(20, 14, 20, 14)
            g.addWidget(StrongBodyLabel(t))
            g.addLayout(box)
            lay.addWidget(gb)
        hc = CardWidget()
        hg = QVBoxLayout(hc)
        hg.setContentsMargins(20, 14, 20, 14)
        hg.addWidget(StrongBodyLabel("大脑体检（智能摘要）"))
        hg.addWidget(self.healthLine)
        lay.addWidget(hc)
        lay.addStretch(1)

    def set_db(self, s: dict):
        """统计页数据源 = 大脑数据库。"""
        self.cardProj.value.setText(str(s.get("projects", 0)))
        self.cardRec.value.setText(str(s.get("records", 0)))
        self.cardIssue.value.setText(str(s.get("errors_open", 0)))
        self.cardStalled.value.setText(str(s.get("projects_stalled", 0)))
        self.cardSearch.value.setText(f"{s.get('searches_today', 0)} / {s.get('searches_total', 0)}")
        try:
            h = brain.health_report(self.win.root)
            dup = f"疑似重复记忆 {h['dup_memory_count']} 组"
            todo = f"待办线索 {h['todo_count']} 条"
            tip = []
            if h['dup_memory_count']:
                tip.append(dup)
            if h['todo_count']:
                tip.append(todo)
            if h['projects_stalled']:
                tip.append(f"停滞项目 {h['projects_stalled']} 个")
            try:
                dc = len(brain.distill_candidates(self.win.root, 5))
            except Exception:
                dc = 0
            if dc:
                tip.append(f"蒸馏候选 {dc} 条")
            self.healthLine.setText(
                f"结晶率 {h['crystallization']}%（记忆/记录）· 检索 今日 {h['searches_today']} / 累计 {h['searches_total']} 次"
                + (" · " + " · ".join(tip) if tip else " · 各项整洁")
                + "（agent 可调 hub_health / hub_distill 看完整报告）")
        except Exception:
            self.healthLine.setText("体检暂不可用")

        def clear(box):
            while box.count():
                it = box.takeAt(0)
                wid = it.widget()
                if wid:  # 先摘出父级再排队销毁：立即从界面消失，不与新一轮内容重叠
                    wid.setParent(None)
                    wid.deleteLater()
                sub = it.layout()
                if sub:  # 行布局（徽章+进度条/文本+按钮）也是布局项，不递归拆净子控件会残留叠加
                    clear(sub)

        clear(self.agentBox)
        total = max(1, s.get("records", 0))
        for agent, n in sorted(s.get("agent_counts", {}).items(), key=lambda x: -x[1]):
            row = QHBoxLayout()
            row.addWidget(badge(agent, agent))
            bar = ProgressBar()
            bar.setValue(round(n / total * 100))
            row.addWidget(bar, 1)
            row.addWidget(BodyLabel(str(n)))
            self.agentBox.addLayout(row)
        if not s.get("agent_counts"):
            self.agentBox.addWidget(CaptionLabel("暂无数据"))

        clear(self.monthBox)
        months = sorted(s.get("monthly_counts", {}).items())[-6:]
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
    KIND_CN = {"record": "记录", "memory": "记忆", "file": "文件", "evfile": "全盘文件"}

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        top = QHBoxLayout()
        self.edit = SearchLineEdit()
        self.edit.setPlaceholderText("全脑检索：工作记录 / 记忆 / 文件名，回车执行…")
        self.edit.setClearButtonEnabled(True)
        top.addWidget(self.edit, 1)
        self.kindFilter = ComboBox()
        self.kindFilter.addItems(["全部", "记录", "记忆", "文件", "全盘文件"])
        self.kindFilter.setFixedWidth(110)
        self.kindFilter.currentIndexChanged.connect(self.refill)
        top.addWidget(self.kindFilter)
        lay.addLayout(top)

        # 左列表右预览可拖分栏：原预览固定 220px 高压在底部，列表只剩几行
        split = QSplitter(Qt.Horizontal)
        self.result = ListWidget()
        split.addWidget(self.result)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(6)
        act = QHBoxLayout()
        act.addStretch(1)
        self.btnOpen = PushButton(ic("LINK", "INFO"), "打开")
        self.btnOpen.clicked.connect(self.open_current)
        act.addWidget(self.btnOpen)
        self.btnLoc = PushButton(ic("FOLDER", "INFO"), "打开所在位置")
        self.btnLoc.clicked.connect(self.open_location_current)
        act.addWidget(self.btnLoc)
        self.btnJump = PushButton(ic("HOME", "INFO"), "跳到项目")
        self.btnJump.clicked.connect(self.jump_project)
        act.addWidget(self.btnJump)
        self.btnCopy = PushButton(ic("COPY", "INFO"), "复制内容")
        self.btnCopy.clicked.connect(self.copy_current)
        act.addWidget(self.btnCopy)
        rv.addLayout(act)
        self.preview = TextBrowser()
        rv.addWidget(self.preview, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
        split.setSizes([520, 420])
        lay.addWidget(split, 1)

        self.hits = []       # 本地命中（kind, data）
        self.view_hits = []  # 当前过滤下显示的命中
        self.ev = {"results": [], "error": ""}  # Everything 全盘段（异步补充）
        self.ev_for = ""
        self._workers = []  # 持住运行中线程引用，防 GC 崩进程
        self.edit.returnPressed.connect(self.run)
        self.result.currentRowChanged.connect(self.show_hit)
        self.result.itemDoubleClicked.connect(lambda _: self.open_current())

    # ---- 数据
    def run(self):
        kw = self.edit.text().strip()
        if not kw or not self.win.root:
            return
        # 本地三查询是毫秒级 SQLite（实测 8/2/1ms），同步跑比 QThread 启动+信号往返更快——
        # 回车立即出结果，不再有"搜索中…"的顿挫
        try:
            res = brain.search_all(self.win.root, kw)
        except Exception as e:  # noqa: BLE001
            res = e
        self.on_done(res)

    def on_done(self, res):
        """本地结果同步填充；Everything 的 es.exe 子进程启动慢，丢后台线程异步追加全盘段。"""
        self.preview.setHtml("")
        self.ev = {"results": [], "error": ""}
        if isinstance(res, Exception):
            self.hits = [("info", f"搜索失败：{res}")]
        else:
            hits = []
            for r in res.get("records", []):
                hits.append(("record", r))
            for m in res.get("memories", []):
                hits.append(("memory", m))
            for f in res.get("files", []):
                hits.append(("file", f))
            self.hits = hits or [("info", "无结果")]
        self.refill()
        kw = self.edit.text().strip()
        self.ev_for = kw
        w = FnWorker(lambda: core.everything_search(kw, 20), self)
        w.done.connect(self._on_everything)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _on_everything(self, ev):
        if self.edit.text().strip() != self.ev_for:
            return  # 用户已改搜索词，这轮全盘结果作废
        self.ev = ev if isinstance(ev, dict) else {"results": [], "error": str(ev)}
        self.refill()

    def refill(self):
        """按类型过滤重建列表。本地在 self.hits，Everything 在 self.ev，显示子集在 self.view_hits。"""
        kind = self.kindFilter.currentIndex()  # 0全部 1记录 2记忆 3文件 4全盘
        key = {1: "record", 2: "memory", 3: "file", 4: "evfile"}.get(kind)
        self.result.clear()
        self.view_hits = []
        if key in (None, "evfile"):
            # Everything 全盘段：es.exe 异步回来才有内容，失败也给一行原因
            if key == "evfile" and self.ev.get("error"):
                self.result.addItem(f"（全盘：{self.ev['error']}）")
                self.view_hits.append(("info", None))
            for pth in self.ev.get("results", []):
                self.result.addItem(f"[全盘] {pth}")
                self.view_hits.append(("evfile", pth))
            if key is None and self.ev.get("error"):
                self.result.addItem(f"── 全盘文件：{self.ev['error']} ──")
                self.view_hits.append(("info", None))
        for k, h in self.hits:
            if key and k != key:
                continue
            if k == "info":
                if key is None:  # 失败/无结果提示只在"全部"里出现
                    self.result.addItem(h)
                    self.view_hits.append(("info", h))
            elif k == "record":
                self.result.addItem(f"[记录] {h['date']} {h['project']}（{h['agent']}）：{h['title'][:80]}")
                self.view_hits.append((k, h))
            elif k == "memory":
                self.result.addItem(f"[记忆#{h['id']}] {brain.KIND_CN.get(h['kind'], h['kind'])}：{h['content'][:100]}")
                self.view_hits.append((k, h))
            elif k == "file":
                self.result.addItem(f"[文件] {h['project']}\\{h['name']}")
                self.view_hits.append((k, h))
        if not self.view_hits and self.hits and self.hits[0][0] != "info":
            self.result.addItem(f"该分类下无结果（全部 {len(self.hits)} 条）")

    def _current(self):
        row = self.result.currentRow()
        if 0 <= row < len(self.view_hits):
            return self.view_hits[row]
        return None, None

    # ---- 交互
    def show_hit(self, row):
        kind, h = self._current() if row >= 0 else (None, None)
        self._sync_actions(kind)
        self.preview.setHtml(self._preview_html(kind, h))
        self._highlight()

    def _preview_html(self, kind, h):
        if kind in (None, "info"):
            return "<p style='color:#888'>点选左侧条目查看详情</p>"
        if kind == "evfile":
            return md_to_html(f"**全盘文件（Everything 检索）**\n\n`{h}`")
        if kind == "record":
            return md_to_html(f"## {h['title']}\n{h['content']}")
        if kind == "memory":
            return md_to_html(f"**{brain.KIND_CN.get(h['kind'], h['kind'])}**"
                              f"{(' #' + h['tags']) if h['tags'] else ''}\n\n{h['content']}")
        return md_to_html(f"**文件**（{h['project']}）\n\n`{h['path']}`")

    def _sync_actions(self, kind):
        """操作按钮按结果类型显隐：能打开的才出现打开按钮，不让按钮灰着占地方。"""
        self.btnOpen.setVisible(kind in ("file", "evfile"))
        self.btnLoc.setVisible(kind in ("file", "evfile"))
        self.btnJump.setVisible(kind == "record")
        self.btnCopy.setVisible(kind in ("record", "memory"))

    def _highlight(self):
        """预览正文里高亮关键词（ExtraSelection 运行时叠加，不改 HTML 结构）。"""
        kw = self.edit.text().strip()
        sels = []
        if kw:
            doc = self.preview.document()
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(255, 214, 90))
            cur = QTextCursor(doc)
            while True:
                cur = doc.find(kw, cur)
                if cur.isNull():
                    break
                sel = QTextEdit.ExtraSelection()
                sel.format = fmt
                sel.cursor = cur
                sels.append(sel)
        self.preview.setExtraSelections(sels)

    def open_current(self):
        kind, h = self._current()
        if kind in ("file", "evfile"):
            p = h["path"] if kind == "file" else h
            if p and Path(p).exists():
                os.startfile(p)
            else:
                InfoBar.warning("文件不存在", str(p)[:120], duration=3000, parent=self.win)
        elif kind == "record":
            self._open_record(h)

    def open_location_current(self):
        kind, h = self._current()
        if kind == "file":
            open_location(h["path"])
        elif kind == "evfile" and Path(h).exists():
            open_location(h)

    def _open_record(self, r):
        """记录落地文件：项目目录下的工作记录.md，没有则打开项目文件夹。"""
        base = Path(self.win.root) / r["project"]
        rec = base / core.RECORD_NAME
        if not self.win.root or not base.exists():
            InfoBar.warning("项目目录不存在", r["project"], duration=3000, parent=self.win)
            return
        os.startfile(rec if rec.is_file() else base)

    def jump_project(self):
        kind, h = self._current()
        if kind == "record" and hasattr(self.win, "project_page"):
            self.win.project_page.select_project(h["project"])
            self.win.switchTo(self.win.project_page)

    def copy_current(self):
        kind, h = self._current()
        if kind == "memory":
            QApplication.clipboard().setText(h["content"])
            InfoBar.success("已复制记忆内容", f"#{h['id']}", duration=2000, parent=self.win)
        elif kind == "record":
            QApplication.clipboard().setText(h["content"])
            InfoBar.success("已复制记录正文", h["title"][:60], duration=2000, parent=self.win)


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

    def set_status(self, status):
        row = self.errList.currentRow()
        if row < 0 or row >= len(self.errors):
            InfoBar.warning("先选择一条错误登记", "", duration=2000, parent=self.win)
            return
        e = self.errors[row]
        err = brain.error_set_status(self.win.root, e["id"], status)
        if err:
            InfoBar.error("操作失败", err, duration=4000, parent=self.win)
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
        # 数字卡点击直达对应页（CardWidget 自带 clicked）
        self.cardProj.clicked.connect(lambda: self.win.switchTo(self.win.project_page))
        self.cardRec.clicked.connect(lambda: self.win.switchTo(self.win.timeline_page))
        self.cardToday.clicked.connect(lambda: self.win.switchTo(self.win.timeline_page))
        self.cardTodo.clicked.connect(self.goto_todo)

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
        g2.addWidget(StrongBodyLabel("Agent 阵容（Agent 中心探测）"))
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
        for text, target in (("打开时间线", "timeline"), ("打开 Agent 中心", "hub"),
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

    def goto_todo(self):
        """待处理卡直达对账分段（2026-10-01 审计补充：数字卡可点击）。"""
        self.win.ledger_page.seg.setCurrentItem("audit")
        self.win.switchTo(self.win.ledger_page)

    def set_snapshot(self, snap):
        self.snap = snap
        self.cardTodo.value.setText(str(len(snap.issues) + len(snap.inbox)))

    def set_db(self, s: dict, recent: list):
        """大脑数据库统计与最近记录（总览的数字/动态改读 brain.db）。"""
        self.cardProj.value.setText(str(s.get("projects", 0)))
        self.cardRec.value.setText(str(s.get("records", 0)))
        self.cardToday.value.setText(str(s.get("today", 0)))

        # 问候语
        h = datetime.now().hour
        greet = "早上好" if h < 12 else ("下午好" if h < 18 else "晚上好")
        self.hello.setText(f"{greet}，{date.today().isoformat()}")

        while self.recentBox.count():
            it = self.recentBox.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.recent = recent
        for r in self.recent:
            row = QHBoxLayout()
            row.addWidget(badge(r.get("agent", ""), ""))
            t = ClickBodyLabel(_display_title(r)[:72])
            t.setToolTip(_display_title(r))
            t.setCursor(Qt.PointingHandCursor)
            t.clicked.connect(lambda _, rr=r: RecordDetailDialog(self.win, rr).exec())
            row.addWidget(t, 1)
            pn = CaptionLabel(r.get("project", "")[:14])
            pn.setToolTip(r.get("project", ""))
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
        if not agents:
            self.agentBox.addWidget(CaptionLabel(
                "（探测中，或未检测到 agent——去「Agent 中心」点「重新探测」，或手动添加 agent 目录）"))
            return
        for a in agents:
            line = CaptionLabel(f"{'●' if a.detected else '○'} {a.name}：技能 {len(a.skills)} · MCP {len(a.mcps)} · 记忆 {len(a.memories)}")
            line.setToolTip(a.home or "未检测到，可在 Agent 中心手动添加目录")
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


# 知名 MCP 服务器目录（名称, 简介, 仓库, 标准配置片段；token/key 留占位由用户自填）
MCP_CATALOG = [
    ("fetch", "网页抓取：把网页转为 Markdown 供模型阅读", "modelcontextprotocol/servers",
     '{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}'),
    ("filesystem", "受控文件系统读写（目录白名单制）", "modelcontextprotocol/servers",
     '{"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/允许的目录"]}}}'),
    ("memory", "知识图谱式长期记忆（跨会话保持）", "modelcontextprotocol/servers",
     '{"mcpServers": {"memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]}}}'),
    ("github", "GitHub 官方 API：仓库 / PR / Issue 全套", "modelcontextprotocol/servers",
     '{"mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], '
     '"env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "<你的token>"}}}}'),
    ("playwright", "浏览器自动化：截图 / 点击 / 表单 / 抓取", "microsoft/playwright-mcp",
     '{"mcpServers": {"playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}}}'),
    ("brave-search", "Brave 联网搜索", "modelcontextprotocol/servers",
     '{"mcpServers": {"brave-search": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-brave-search"], '
     '"env": {"BRAVE_API_KEY": "<你的key>"}}}}'),
    ("sqlite", "SQLite 数据库直查", "modelcontextprotocol/servers",
     '{"mcpServers": {"sqlite": {"command": "uvx", "args": ["mcp-server-sqlite", "--db-path", "D:/数据/库名.db"]}}}'),
    ("sequential-thinking", "结构化分步推理（动态思考链）", "modelcontextprotocol/servers",
     '{"mcpServers": {"sequential-thinking": {"command": "npx", '
     '"args": ["-y", "@modelcontextprotocol/server-sequential-thinking"]}}}'),
    ("tavily", "Tavily 联网搜索与网页抽取", "tavily-ai/tavily-mcp",
     '{"mcpServers": {"tavily": {"command": "npx", "args": ["-y", "tavily-mcp@latest"], '
     '"env": {"TAVILY_API_KEY": "<你的key>"}}}}'),
    ("everything", "官方测试服务器：验证 MCP 接入是否连通", "modelcontextprotocol/servers",
     '{"mcpServers": {"everything": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-everything"]}}}'),
]


class MarketPage(QWidget):
    """能力市场：技能双源安装（本地缓存 / 官方实时 + identifier 直装）+ MCP 目录片段复制。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.agents: list = []
        self.market_items: list = []
        self._mcp_entries: list = []
        self._market_dirs: list = []
        self._workers = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        top = QHBoxLayout()
        top.addWidget(SubtitleLabel("能力市场"))
        top.addStretch(1)
        lay.addLayout(top)
        lay.addWidget(CaptionLabel(
            "技能：从索引源安装到任意 agent 技能库，或粘贴 identifier 直装任意 GitHub 仓库技能；"
            "MCP：知名服务器目录，复制配置片段后自行粘贴到对应 agent 的 MCP 配置（本软件不自动写入，密钥不落库）。"))

        self.seg = SegmentedWidget(self)
        self.seg.addItem("skill", "技能市场")
        self.seg.addItem("mcp", "MCP 目录")
        self.seg.currentItemChanged.connect(
            lambda key: self.stack.setCurrentIndex({"skill": 0, "mcp": 1}.get(key, 0)))
        self.stack = QStackedWidget(self)
        lay.addWidget(self.seg)
        lay.addWidget(self.stack, 1)

        # ---- 技能市场页
        sk = QWidget()
        g = QVBoxLayout(sk)
        g.setContentsMargins(0, 8, 0, 0)
        g.setSpacing(8)
        row = QHBoxLayout()
        row.addWidget(CaptionLabel("来源"))
        self.srcCombo = ComboBox()
        self.srcCombo.addItems(["本地缓存索引（anthropics/skills，离线）", "官方实时索引（GitHub，需网络）"])
        self.srcCombo.setFixedWidth(300)
        self.srcCombo.currentIndexChanged.connect(self.reload_market)
        row.addWidget(self.srcCombo)
        self.filterEdit = SearchLineEdit()
        self.filterEdit.setPlaceholderText("过滤技能…")
        self.filterEdit.setFixedWidth(220)
        self.filterEdit.textChanged.connect(self.fill_market)
        row.addWidget(self.filterEdit)
        row.addStretch(1)
        refreshBtn = PushButton(ic("SYNC", "INFO"), "刷新索引")
        refreshBtn.clicked.connect(self.reload_market)
        row.addWidget(refreshBtn)
        g.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(CaptionLabel("安装到"))
        self.marketTargets = ComboBox()
        self.marketTargets.setFixedWidth(250)
        row2.addWidget(self.marketTargets)
        installBtn = PrimaryPushButton(ic("DOWNLOAD", "INFO"), "安装选中技能")
        installBtn.clicked.connect(self.install_selected)
        row2.addWidget(installBtn)
        self.idEdit = LineEdit()
        self.idEdit.setPlaceholderText("或粘贴 identifier 直装：仓库/路径/技能名")
        row2.addWidget(self.idEdit, 1)
        idBtn = PushButton(ic("DOWNLOAD", "INFO"), "直装")
        idBtn.clicked.connect(self.install_identifier)
        row2.addWidget(idBtn)
        g.addLayout(row2)

        self.marketList = ListWidget()
        g.addWidget(self.marketList, 1)
        self.stack.addWidget(sk)

        # ---- MCP 目录页
        mp = QWidget()
        m = QVBoxLayout(mp)
        m.setContentsMargins(0, 8, 0, 0)
        m.setSpacing(8)
        mrow = QHBoxLayout()
        self.mcpFilter = SearchLineEdit()
        self.mcpFilter.setPlaceholderText("过滤 MCP…")
        self.mcpFilter.setFixedWidth(220)
        self.mcpFilter.textChanged.connect(self.fill_mcp_catalog)
        mrow.addWidget(self.mcpFilter)
        mrow.addStretch(1)
        copyBtn = PushButton(ic("COPY", "INFO"), "复制配置片段")
        copyBtn.clicked.connect(self.copy_mcp_config)
        mrow.addWidget(copyBtn)
        m.addLayout(mrow)
        self.mcpList = ListWidget()
        m.addWidget(self.mcpList, 1)
        self.mcpPreview = TextBrowser()
        self.mcpPreview.setMaximumHeight(150)
        self.mcpPreview.setHtml("<p style='color:#888'>点选上方条目查看配置片段</p>")
        m.addWidget(self.mcpPreview)
        self.stack.addWidget(mp)

        self.marketList.itemClicked.connect(self.preview_market)
        self.mcpList.itemClicked.connect(self.preview_mcp)
        self.fill_mcp_catalog()
        QTimer.singleShot(300, self.reload_market)

    # ---- 数据
    def set_agents(self, agents):
        """由 Agent 中心探测完成后同步（复用探测结果，不重复扫描）。"""
        self.agents = agents
        self._fill_targets()

    def _fill_targets(self):
        cur = self.marketTargets.currentData()
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
        if cur:
            i = self.marketTargets.findData(cur)
            if i >= 0:
                self.marketTargets.setCurrentIndex(i)

    def reload_market(self):
        self.marketList.clear()
        self.marketList.addItem("（加载索引中…）")
        src = self.srcCombo.currentIndex()
        w = FnWorker(lambda: agentscore.load_local_market() if src == 0
                     else agentscore.fetch_remote_skills(), self)
        w.done.connect(self._on_market_loaded)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _on_market_loaded(self, result):
        self.marketList.clear()
        if isinstance(result, Exception):
            self.marketList.addItem(f"（索引加载失败：{result}）")
            return
        self.market_items = result
        self.fill_market()

    def fill_market(self):
        kw = self.filterEdit.text().strip().lower()
        self.marketList.clear()
        for mitem in self.market_items:
            if kw and kw not in mitem["name"].lower() and kw not in mitem["desc"].lower():
                continue
            self.marketList.addItem(f"{mitem['name']}  ——  {mitem['desc'][:80]}")
            self.marketList.item(self.marketList.count() - 1).setData(
                Qt.UserRole, mitem["identifier"])
        if not self.marketList.count():
            self.marketList.addItem("（无匹配技能）")

    def fill_mcp_catalog(self):
        kw = self.mcpFilter.text().strip().lower()
        self.mcpList.clear()
        self._mcp_entries = []
        for entry in MCP_CATALOG:
            name, desc = entry[0], entry[1]
            if kw and kw not in name.lower() and kw not in desc.lower():
                continue
            self.mcpList.addItem(f"{name}  ——  {desc}")
            self._mcp_entries.append(entry)
        if not self.mcpList.count():
            self.mcpList.addItem("（无匹配）")

    # ---- 交互
    def preview_market(self, item):
        row = self.marketList.currentRow()
        if not (0 <= row < len(self.market_items)):
            return
        mitem = self.market_items[row]
        if mitem["desc"] and mitem["desc"] != "（点选预览详情）":
            return  # 本地缓存源已带描述
        item.setText(f"{mitem['name']}  ——  详情加载中…")
        w = FnWorker(lambda: agentscore.fetch_skill_desc(mitem["identifier"]), self)
        w.done.connect(lambda d, it=item, mi=mitem: it.setText(
            f"{mi['name']}  ——  {(str(d) if d and not isinstance(d, Exception) else '（暂无详情）')[:80]}"))
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def preview_mcp(self, item):
        row = self.mcpList.currentRow()
        if not (0 <= row < len(self._mcp_entries)):
            return
        name, desc, repo, cfg = self._mcp_entries[row]
        self.mcpPreview.setHtml(
            f"<p><b>{name}</b> — {desc}</p><p style='color:#888'>仓库：{repo}</p>"
            f"<pre style='background:#f5f5f5;padding:8px'>{html.escape(cfg)}</pre>")

    def copy_mcp_config(self):
        row = self.mcpList.currentRow()
        if not (0 <= row < len(self._mcp_entries)):
            InfoBar.warning("先选择一个 MCP 服务器", "", duration=2000, parent=self.win)
            return
        QApplication.clipboard().setText(self._mcp_entries[row][3])
        InfoBar.success("配置片段已复制", "粘贴到对应 agent 的 MCP 配置文件（本软件不自动写入）",
                        duration=3000, parent=self.win)

    def install_selected(self):
        row = self.marketList.currentRow()
        if row < 0 or row >= len(self.market_items):
            InfoBar.warning("先在列表中选择一个技能", "", duration=2000, parent=self.win)
            return
        mitem = self.market_items[row]
        self._install(mitem["name"], mitem["identifier"])

    def install_identifier(self):
        ident = self.idEdit.text().strip().strip("/")
        if not ident:
            InfoBar.warning("先粘贴技能 identifier", "", duration=2000, parent=self.win)
            return
        if ident.count("/") < 3:
            InfoBar.warning("格式应为：仓库拥有者/仓库名/路径/技能名", "", duration=3000, parent=self.win)
            return
        self._install(ident.split("/")[-1], ident)

    def _install(self, name: str, identifier: str):
        target = self.marketTargets.currentData()
        if not target:
            return
        InfoBar.info("开始安装", f"{name}（需网络/代理）", duration=2500, parent=self.win)
        w = FnWorker(lambda: agentscore.install_skill(identifier, target), self)
        w.done.connect(self._on_installed)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _on_installed(self, result):
        text = str(result)
        if text.startswith("已安装"):
            InfoBar.success("安装完成", text, duration=4000, parent=self.win)
            if getattr(self.win, "hub_page", None):
                self.win.hub_page.rescan()
        else:
            InfoBar.error("安装失败", text[:120], duration=6000, parent=self.win)


class HubPage(QWidget):
    """Agent 中心：聚合电脑上各 agent 的技能 / MCP / 记忆 / 全局配置；
    记忆可增删改，配置文件可编辑（自动备份）。"""

    VIEWS = ["技能库", "MCP 服务器", "记忆", "全局配置", "环境档案"]

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.agents: list = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)

        top = QHBoxLayout()
        top.addWidget(SubtitleLabel("Agent 中心"))
        top.addStretch(1)
        addBtn = PushButton(ic("ADD", "INFO"), "添加 agent 目录")
        addBtn.clicked.connect(self.add_agent)
        top.addWidget(addBtn)
        envBtn = PushButton(ic("IOT", "INFO"), "采集本机配置")
        envBtn.clicked.connect(self.scan_env)
        top.addWidget(envBtn)
        rescanBtn = PrimaryPushButton(ic("SYNC", "INFO"), "重新探测")
        rescanBtn.clicked.connect(self.rescan)
        top.addWidget(rescanBtn)
        lay.addLayout(top)
        lay.addWidget(CaptionLabel("自动探测本机 agent 及其技能 / MCP / 记忆 / 全局配置；"
                                   "记忆可增删改，配置文件可编辑保存（自动备份）；MCP 出于安全只读。"
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
        self.memFilter = SearchLineEdit()
        self.memFilter.setPlaceholderText("过滤记忆（内容/标签）…")
        self.memFilter.setFixedWidth(260)
        self.memFilter.textChanged.connect(self.fill_memories)
        viewRow.addWidget(self.memFilter)
        self.envFilter = SearchLineEdit()
        self.envFilter.setPlaceholderText("搜索环境配置…")
        self.envFilter.setFixedWidth(260)
        self.envFilter.textChanged.connect(self.fill_env)
        viewRow.addWidget(self.envFilter)
        self.memFilter.hide()  # 仅记忆视图显示
        self.envFilter.hide()  # 仅环境档案视图显示
        lay.addLayout(viewRow)

        self.stack = QStackedWidget()
        self.skillList = ListWidget()
        self.mcpList = ListWidget()
        self.memList = ListWidget()
        self.cfgList = ListWidget()
        self.envList = ListWidget()
        for w in (self.skillList, self.mcpList, self.memList, self.cfgList, self.envList):
            self.stack.addWidget(w)
            w.itemClicked.connect(self.on_preview)
            w.itemDoubleClicked.connect(self.on_open)
            w.setContextMenuPolicy(Qt.CustomContextMenu)
            w.customContextMenuRequested.connect(self._ctx_menu)
        self.viewCombo.currentIndexChanged.connect(self._on_view_changed)
        lay.addWidget(self.stack, 1)

        lay.addWidget(CaptionLabel("单击预览 · 双击打开所在目录 · 右键更多操作 · MCP 出于安全只显示名称与来源，不显示密钥内容"))

        editRow = QHBoxLayout()
        self.editBtn = PushButton(ic("EDIT", "INFO"), "编辑并保存（自动备份）")
        self.editBtn.setEnabled(False)
        self.editBtn.clicked.connect(self.edit_current)
        editRow.addWidget(self.editBtn)
        editRow.addStretch(1)
        newMemBtn = PushButton(ic("ADD", "INFO"), "新建记忆")
        newMemBtn.clicked.connect(self.new_memory)
        editRow.addWidget(newMemBtn)
        delMemBtn = PushButton(ic("DELETE", "INFO"), "删除选中记忆")
        delMemBtn.clicked.connect(self.delete_memory)
        editRow.addWidget(delMemBtn)
        lay.addLayout(editRow)

        self.preview = TextBrowser()
        self.preview.setMaximumHeight(240)
        self.preview.setHtml("<p style='color:#888'>点击左侧列表项预览</p>")
        lay.addWidget(self.preview)

        self._workers = []
        QTimer.singleShot(300, self.rescan)

    # ---- 数据
    def _on_view_changed(self, idx):
        """视图切换：stack 翻页 + 过滤框按视图显隐（技能/记忆/环境档案各有过滤框）。"""
        self.stack.setCurrentIndex(idx)
        self.skillFilter.setVisible(idx == 0)
        self.memFilter.setVisible(idx == 2)
        self.envFilter.setVisible(idx == 4)

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
        try:
            self._reg = {r["name"].lower(): r for r in brain.list_agents(self.win.root)}
        except Exception:
            self._reg = {}
        self.rebuild_cards()
        self.fill_skills()
        self.fill_mcps()
        self.fill_memories()
        self.fill_assets(self.cfgList, [(a.name, c) for a in self.agents for c in a.configs])
        self.preview.setHtml("<p style='color:#888'>点选左侧技能 / MCP / 记忆查看详情，双击打开所在目录</p>")
        self.win.overview_page.set_agents(self.agents)
        InfoBar.success("探测完成", f"{len(self.agents)} 个 agent", duration=2000, parent=self.win)

    def fill_memories(self):
        """大脑记忆视图：SQLite memories 表（条目 data = mem:<id>），支持内容/标签过滤。"""
        self.memList.clear()
        self._mem_rows = []
        kw = self.memFilter.text().strip().lower() if hasattr(self, "memFilter") else ""
        try:
            self._mem_rows = brain.search_memories(self.win.root, "", "", 500)
        except Exception:
            pass
        for m in self._mem_rows:
            if kw and kw not in m["content"].lower() and kw not in (m["tags"] or "").lower():
                continue
            flag = "★" if m["pinned"] else "·"
            tags = f" #{m['tags']}" if m["tags"] else ""
            src = f" @{m['agent']}" if m["agent"] and m["agent"] != "migrated" else ""
            self.memList.addItem(f"{flag} #{m['id']} [{brain.KIND_CN.get(m['kind'], m['kind'])}]{tags}{src}  {m['content'][:90]}")
            self.memList.item(self.memList.count() - 1).setData(Qt.UserRole, f"mem:{m['id']}")
        if not self._mem_rows:
            self.memList.addItem("（大脑记忆为空——用下方「新建记忆」或让 agent 调 hub_memory_write）")
            self.memList.item(0).setData(Qt.UserRole, "")
        elif not self.memList.count():
            self.memList.addItem("（无匹配记忆）")
            self.memList.item(0).setData(Qt.UserRole, "")

    def fill_env(self):
        """环境档案视图：本机配置结构化登记（brain.env_items）。"""
        self.envList.clear()
        kw = self.envFilter.text().strip().lower() if hasattr(self, "envFilter") else ""
        try:
            rows = brain.env_list(self.win.root, "", kw, 300)
        except Exception:
            rows = []
        for r in rows:
            self.envList.addItem(f"[{r['category']}] {r['key']} = {r['value'][:70]}")
            self.envList.item(self.envList.count() - 1).setData(
                Qt.UserRole, f"env:{r['category']}|{r['key']}")
        if not rows:
            self.envList.addItem("（环境档案为空——点上方「采集本机配置」自动建档，或让 agent 调 hub_env_set）")
            self.envList.item(0).setData(Qt.UserRole, "")

    def scan_env(self):
        """自动采集本机配置快照写入环境档案（幂等 UPSERT）。"""
        if not self.win.root:
            InfoBar.warning("先在设置页选择根目录", "", duration=2500, parent=self.win)
            return
        InfoBar.info("采集本机配置中", "标准库只读采集，秒级完成", duration=2000, parent=self.win)

        def _do():
            n = brain.env_scan(self.win.root, agent="user")
            core.journal(self.win.root, "user", "env_scan", f"{n} 项")
            return n

        w2 = FnWorker(_do, self)
        w2.done.connect(self._on_env_scan)
        w2.finished.connect(lambda: self._workers.remove(w2) if w2 in self._workers else None)
        self._workers.append(w2)
        w2.start()

    def _on_env_scan(self, result):
        if isinstance(result, Exception):
            InfoBar.error("采集失败", str(result), duration=4000, parent=self.win)
            return
        InfoBar.success("环境档案已更新", f"{result} 项配置已登记", duration=3000, parent=self.win)
        self.viewCombo.setCurrentIndex(4)
        self.fill_env()

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
            reg = getattr(self, "_reg", {}).get(a.name.lower())
            if reg:
                g.addWidget(CaptionLabel(
                    f"{'● 在线' if reg['online'] else '○ 离线'} · 记录 {reg['records']} 条 · "
                    f"最近活跃 {reg['last_seen'] or '—'}"))
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
        if not path:
            self.preview.setHtml("<p style='color:#888'>无内容</p>")
            return
        # 环境档案条目（data = env:category|key）
        if isinstance(path, str) and path.startswith("env:"):
            self.editBtn.setEnabled(False)
            self.editBtn.setText("环境配置项（agent 经 hub_env_set 更新）")
            cat, key = path[4:].split("|", 1)
            rows = brain.env_list(self.win.root, cat, key, 1)
            if rows:
                self.preview.setHtml(md_to_html(
                    f"**[{rows[0]['category']}] {rows[0]['key']}**\n\n{rows[0]['value']}"
                    f"\n\n更新于 {rows[0]['updated'][:10]}（{rows[0]['agent'] or '-'} 登记）"))
            return
        # 大脑记忆条目（data = mem:<id>）
        if isinstance(path, str) and path.startswith("mem:"):
            self.editBtn.setEnabled(True)
            self.editBtn.setText("编辑这条记忆")
            m = next((x for x in self._mem_rows if x["id"] == int(path[4:])), None)
            if m:
                meta = f"类型 {brain.KIND_CN.get(m['kind'], m['kind'])} · 置顶{'是' if m['pinned'] else '否'}" \
                       f" · 标签 {m['tags'] or '无'} · {m['created']}"
                self.preview.setHtml(f"<p style='color:#888'>{meta}</p>" + md_to_html(m["content"]))
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
            self.preview.setHtml("<p style='color:#888'>(文件尚不存在，编辑保存后创建)</p>")
            self.cur_editable = False

    def edit_current(self):
        if not self.cur_path:
            return
        if isinstance(self.cur_path, str) and self.cur_path.startswith("mem:"):
            mid = int(self.cur_path[4:])
            m = next((x for x in self._mem_rows if x["id"] == mid), None)
            if not m:
                return
            dlg = MemoryEditDialog(self.win, m)
            if dlg.exec():
                err = brain.edit_memory(root=self.win.root, mid=mid, content=dlg.content(),
                                        kind=dlg.kind(), tags=dlg.tags(), pinned=dlg.pinned())
                if err:
                    InfoBar.error("保存失败", err, duration=4000, parent=self.win)
                else:
                    core.journal(self.win.root, "user", "memory_edit", f"brain:memories#{mid}")
                    InfoBar.success("记忆已更新", "", duration=2000, parent=self.win)
                    self.fill_memories()
            return
        if not self.cur_editable:
            return
        dlg = EditAssetDialog(self.win, self.cur_path, Path(self.cur_path).name)
        if dlg.exec():
            err = core.write_text_backed(self.cur_path, dlg.text(), root=self.win.root, agent="user")
            if err:
                InfoBar.error("保存失败", err, duration=4000, parent=self.win)
            else:
                InfoBar.success("已保存（原文件已备份）", "", duration=2500, parent=self.win)

    def new_memory(self):
        if not self.win.root:
            InfoBar.warning("先在设置页选择根目录", "", duration=2500, parent=self.win)
            return
        dlg = MemoryEditDialog(self.win, None)
        if dlg.exec():
            mid = brain.add_memory(self.win.root, dlg.content().strip(), dlg.kind(),
                                   dlg.tags(), agent="user", pinned=dlg.pinned())
            core.journal(self.win.root, "user", "memory_append", f"brain:memories#{mid}", note=dlg.kind())
            InfoBar.success("已写入大脑记忆", f"#{mid}", duration=2500, parent=self.win)
            self.fill_memories()

    def delete_memory(self):
        if not self.cur_path or not (isinstance(self.cur_path, str) and self.cur_path.startswith("mem:")):
            InfoBar.warning("先在列表中选择一条记忆", "", duration=2500, parent=self.win)
            return
        mid = int(self.cur_path[4:])
        err = brain.delete_memory(self.win.root, mid)
        if err:
            InfoBar.error("删除失败", err, duration=4000, parent=self.win)
        else:
            core.journal(self.win.root, "user", "memory_delete", f"brain:memories#{mid}")
            InfoBar.success("已删除（软删，可整库追溯）", "", duration=2500, parent=self.win)
            self.fill_memories()

    def on_open(self, item):
        path = item.data(Qt.UserRole)
        if path:
            open_location(path, select=False)

    def _ctx_menu(self, pos):
        lst = self.sender()
        item = lst.itemAt(pos)
        if item is None:
            return
        lst.setCurrentItem(item)
        data = item.data(Qt.UserRole)
        menu = QMenu(self)
        act_open = menu.addAction("打开所在目录")
        act_edit = act_del = None
        if isinstance(data, str) and data.startswith("mem:"):
            act_edit = menu.addAction("编辑这条记忆")
            act_del = menu.addAction("删除这条记忆")
        elif (isinstance(data, str) and data and self.stack.currentIndex() != 1
              and Path(data).is_file()):
            act_edit = menu.addAction("编辑文件（自动备份）")
        act_copy = menu.addAction("复制路径")
        chosen = menu.exec(QCursor.pos())
        if chosen is act_open:
            self.on_open(item)
        elif chosen is act_edit:
            self.edit_current()
        elif chosen is act_del:
            self.delete_memory()
        elif chosen is act_copy and data:
            QApplication.clipboard().setText(str(data))
            InfoBar.success("已复制", "", duration=1200, parent=self.win)

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
            "AgentHub 通过标准 MCP 协议向所有 agent 提供 25 个工具：项目登记/新建、工作记录（可撤销）、"
            "全文搜索、团队规范、公用记忆读写、会话心跳防撞车、错误登记与查询、撤销、踩坑拦截、归档、技能/MCP 清单、进度对齐。"
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
    <li><b>搜索</b>：全脑检索（记录/记忆/文件分类过滤），回车即出结果，结果可直接打开或跳转，Ctrl+F 直达。</li>
    <li><b>Agent 中心</b>：各 agent 的技能库 / MCP / 记忆 / 全局配置聚合，含注册档案（在线状态/累计记录）；记忆可增删改、配置可编辑（自动备份）。</li>
    <li><b>大脑</b>：唤起排行 / 使用分布 / 待办看板（一键勾销）/ 置顶记忆，对准大脑验收四条标准。</li>
    <li><b>接入</b>：一键写 MCP 配置 + 注入开工引导到 agent 全局指令文件（均自动备份、可移除）；hermes 等复制配置片段手动粘贴。</li>
    <li><b>对账</b>：揪出 agent 前缀平行目录、重复项目、野目录，杜绝记录分裂。</li>
    </ul>
    <h2>agent 怎么接入公用大脑（两步）</h2>
    <p>1. 到「接入」页对某个 agent 点「一键接入」+「注入引导」（ZCode / Claude Code / Codex / DSH 支持）；<br>
    2. 重启对应 agent——它每次开工就会自动读进度和记忆、干完活自动写记录、出错自动登记，无需口头提醒。</p>
    <h2>公用记忆是什么</h2>
    <p>Agent 中心 → 记忆 → 第一条「共享记忆」。所有已接入的 agent 都能读写（MCP 工具 hub_memory_read / hub_memory_write）。
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
        g1b.addWidget(StrongBodyLabel("大脑数据库（_hub\\brain.db —— 记忆/记录/流水的唯一真理）"))
        dbRow = QHBoxLayout()
        self.backupBtn = PrimaryPushButton(ic("SAVE", "INFO"), "立即备份大脑")
        self.backupBtn.clicked.connect(self.backup_now)
        dbRow.addWidget(self.backupBtn)
        self.dbLabel = CaptionLabel("")
        dbRow.addWidget(self.dbLabel, 1)
        g1b.addLayout(dbRow)
        g1b.addWidget(CaptionLabel("每次退出软件自动备份（保留 30 份，在 _hub\\brain-backups\\）；"
                                   "历史 md/jsonl 已增量迁移入库并保留为只读存档。"))
        lay.addWidget(c1b)

        c1c = CardWidget()
        g1c = QVBoxLayout(c1c)
        g1c.setContentsMargins(20, 14, 20, 14)
        g1c.addWidget(StrongBodyLabel("窗口大小（点一下立即生效，边缘拖拽外的一键兜底）"))
        sizeRow = QHBoxLayout()
        for label, wd, ht in (("紧凑 1180×760", 1180, 760), ("标准 1400×900", 1400, 900),
                              ("宽敞 1616×950", 1616, 950)):
            b = PushButton(label)
            b.clicked.connect(lambda _, w_=wd, h_=ht: self.win.showNormal() or self.win.resize(w_, h_))
            sizeRow.addWidget(b)
        sizeRow.addStretch(1)
        g1c.addLayout(sizeRow)
        g1c.addWidget(CaptionLabel("也可以用键盘：Win+←/→ 贴靠半屏，Alt+空格→大小 用方向键精调。"))
        lay.addWidget(c1c)

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

    def backup_now(self):
        if not self.win.root:
            InfoBar.warning("先选择根目录", "", duration=2000, parent=self.win)
            return
        err = brain.backup_brain(self.win.root)
        if err.startswith("_") or "失败" in err:
            InfoBar.error("备份失败", err, duration=4000, parent=self.win)
        else:
            self.dbLabel.setText(f"上次备份：{err}")
            InfoBar.success("大脑已备份", err, duration=3000, parent=self.win)

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
        core.JOURNAL_SINK = brain.journal_add  # 流水落库
        self._workers = []
        if self.root and os.path.isdir(self.root):
            # 建表 + 历史 md/jsonl 增量迁移（幂等），后台跑防卡启动
            w = FnWorker(lambda: brain.init_db(self.root), self)
            w.done.connect(self._on_db_ready)
            self._workers.append(w)
            w.start()

        self.overview_page = OverviewPage(self)
        self.project_page = ProjectPage(self)
        self.timeline_page = TimelinePage(self)
        self.stats_page = StatsPage(self)
        self.brain_page = BrainPage(self)
        self.search_page = SearchPage(self)
        self.ledger_page = LedgerPage(self)
        self.audit_page = self.ledger_page.audit      # 原对账页别名，apply() 链路不变
        self.journal_page = self.ledger_page.journal  # 原流水页别名，reload 链路不变
        self.hub_page = HubPage(self)
        self.connect_page = ConnectPage(self)
        self.help_page = HelpPage(self)
        self.settings_page = SettingsPage(self)

        # Fluent 导航规范：主区只放"地方"，按语义分组（工作区/数据），配置与帮助类沉底，
        # 避免 11 项平铺扫视困难（2026-10-01 参照官方示例与 MS NavigationView 指南重整）
        self.overview_page.setObjectName("总览")
        self.addSubInterface(self.overview_page, ic("HOME"), "总览")

        self.navigationInterface.addItemHeader("工作区")
        for w, icon, text in (
            (self.project_page, "FOLDER", "项目"),
            (self.timeline_page, "HISTORY", "时间线"),
            (self.hub_page, "PEOPLE", "Agent 中心"),
        ):
            w.setObjectName(text)
            self.addSubInterface(w, ic(icon), text)

        self.navigationInterface.addItemHeader("数据")
        for w, icon, text in (
            (self.brain_page, "LIBRARY", "大脑"),
            (self.ledger_page, "DICTIONARY", "流水·对账"),
            (self.stats_page, "TILES", "统计"),
            (self.search_page, "SEARCH", "搜索"),
        ):
            w.setObjectName(text)
            self.addSubInterface(w, ic(icon), text)

        for w, icon, text in (
            (self.connect_page, "LINK", "接入"),
            (self.help_page, "INFO", "帮助"),
            (self.settings_page, "SETTING", "设置"),
        ):
            w.setObjectName(text)
            self.addSubInterface(w, ic(icon), text, NavigationItemPosition.BOTTOM)

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
        self._workers = getattr(self, "_workers", [])  # 迁移线程已在 __init__ 挂入
        QTimer.singleShot(200, self.refresh)
        if not self.root:
            self.switchTo(self.settings_page)

    def _on_db_ready(self, result):
        """大脑库就绪（含历史迁移结果）。"""
        if isinstance(result, Exception):
            InfoBar.error("大脑数据库初始化失败", str(result), duration=6000, parent=self)
            return
        if result:
            InfoBar.warning("大脑建库告警", str(result), duration=6000, parent=self)
        self.refresh()  # 迁移完成后重刷，各页补上 DB 数据

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
            nav.setExpandWidth(170)  # v2.1 导航更名后（Agent 中心/流水·对账）96px 截断文字
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
        """退出前：断后台线程信号（防 wait 超时后回调触碰已关闭的 UI）、等线程结束，备份大脑，再记住状态。"""
        for lst in (self._workers, getattr(self.hub_page, "_workers", []),
                    getattr(self.search_page, "_workers", [])):
            for t in list(lst):
                try:
                    t.blockSignals(True)  # 阻断 done/finished 全部信号：wait 超时后线程回调不再进已关闭的界面
                    t.wait(2000)
                except Exception:
                    pass
        if self.root and os.path.isdir(self.root):
            try:
                brain.backup_brain(self.root)  # 退出自动备份大脑（轮转保留30份）
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
        # 退出挂起兜底（qfw item view+QSS 偶发挂起，os._exit 兜底须 exec 返回才生效）：
        # 走到这里备份/保存已全部完成，若 2 秒后事件循环仍未退出就硬杀，不给用户留"关不掉"
        QTimer.singleShot(2000, lambda: os._exit(0) if not self.isVisible() else None)

    def goto_search(self):
        self.switchTo(self.search_page)
        self.search_page.edit.setFocus()

    def set_root(self, d):
        self.root = d
        core.set_root(d)
        core.init_hub(d)
        err = brain.init_db(d)
        if err:
            InfoBar.error("大脑建库失败", err, duration=5000, parent=self)
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
        # 文件索引刷进大脑（供全脑检索按文件名命中）
        rows = []
        for p in snap.projects:
            for group, rp, fp in p.files:
                try:
                    st = os.stat(fp)
                    rows.append((p.name, fp, Path(fp).name, group, st.st_size, st.st_mtime))
                except OSError:
                    continue
        try:
            brain.update_files_index(self.root, rows)
        except Exception:
            pass
        self.project_page.set_snapshot(snap)
        try:
            self.timeline_page.set_records(brain.list_records(self.root, limit=2000))
            self.stats_page.set_db(brain.stats(self.root))
            self.brain_page.reload()
        except Exception:
            pass
        self.audit_page.set_snapshot(snap)
        self.overview_page.set_snapshot(snap)
        try:
            self.overview_page.set_db(brain.stats(self.root), brain.list_records(self.root, limit=8))
            self.overview_page.set_sessions(brain.active_sessions(self.root))
        except Exception:
            self.overview_page.set_sessions([])
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
