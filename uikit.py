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

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QEvent, QSize, QAbstractNativeEventFilter
from PySide6.QtGui import (QShortcut, QKeySequence, QFont, QCursor, QColor, QTextCharFormat,
                          QTextCursor, QIcon, QGuiApplication)
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit, QMenu, QSizePolicy,
                               QVBoxLayout, QWidget, QHeaderView, QAbstractItemView,
                               QStackedWidget, QSizeGrip, QSplitter, QTextEdit, QPushButton,
                               QDialog, QCheckBox, QCompleter)
from qfluentwidgets import (BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox,
                            FluentIcon as FIF,
                            FluentWindow, IconWidget, InfoBar, LineEdit, ListWidget, MessageBox,
                            MessageBoxBase,
                            NavigationItemPosition, PrimaryPushButton, ProgressBar, PushButton,
                            ScrollArea, SearchLineEdit, SegmentedWidget, StrongBodyLabel,
                            SubtitleLabel, TextBrowser, TextEdit, TitleLabel, qconfig, setTheme,
                            Theme, isDarkTheme)

import agentscore
import brain
import core
import ops
import palette
import report
import tokens


def ic(name, fallback="INFO"):
    """图标枚举兜底，防不同版本枚举名缺失导致崩。"""
    return getattr(FIF, name, getattr(FIF, fallback))


def _page_head(title: str, desc: str = "") -> QWidget:
    """统一页头（批次 3B 每页定位）：Title + 一句话说明。

    定位语 = 这页回答什么问题（10-07 提案③）。
    沿用 qfw 默认色（随主题自适应），不设硬编码。
    """
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    lay.addWidget(TitleLabel(title))
    if desc:
        d = CaptionLabel(desc)
        d.setWordWrap(True)
        lay.addWidget(d)
    return box


def empty_state(icon_name: str, title: str, desc: str = "",
                action_text: str = "", action=None) -> QWidget:
    """统一空态（批次 3C）：图标 + 标题 + 说明 + 可选动作（shadcn Empty 结构）。

    三类空态文案分开写（学习报告一）：
      首次空=教（说明+CTA）/ 清空空=确认 / 筛选空=恢复路径。
    用 qfw 组件默认色，随主题自适应。
    """
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 20, 0, 12)
    lay.setSpacing(4)
    try:
        iw = IconWidget(ic(icon_name), box)
        iw.setFixedSize(26, 26)
        lay.addWidget(iw, 0, Qt.AlignHCenter)
    except Exception:  # noqa: BLE001 图标失败不阻断
        pass
    t = StrongBodyLabel(title, box)
    t.setAlignment(Qt.AlignCenter)
    lay.addWidget(t)
    if desc:
        d = CaptionLabel(desc, box)
        d.setAlignment(Qt.AlignCenter)
        d.setWordWrap(True)
        lay.addWidget(d)
    if action and action_text:
        b = PushButton(action_text, box)
        b.clicked.connect(action)
        lay.addWidget(b, 0, Qt.AlignHCenter)
    return box


# 可定制导航项（3E+）：设置页勾选控制侧边栏显隐；总览/设置固定不可藏（隐藏页仍可 Ctrl+K 打开）
NAV_CUSTOMIZABLE = ("项目", "时间线", "Agent 中心", "随记", "大脑",
                    "流水·对账", "统计", "运营报告", "搜索", "接入", "帮助")


def flex_label(lb):
    """长文本行标签防撑宽（批次 3 修复）：水平 Ignored = 布局按可用空间分配、超出裁剪
    （全文见 tooltip）。背景：最近动态/时间线标题的无换行 QLabel 把页面最小宽度撑到
    2000px+，窄窗口下数字卡等被横向挤出（2026-10-10 实机截图像素级定位）。
    注：裁剪 = 无省略号的硬切；配合 ElidedLabel/ClickBodyLabel（自适应省略）使用体验最佳。"""
    lb.setSizePolicy(QSizePolicy.Ignored, lb.sizePolicy().verticalPolicy())
    return lb


def guard_overflow(root_widget):
    """防横向溢出守卫（批次 3 修复）：信息类长文本标签设水平 Ignored（幂等）。

    背景：8 个页面存在"无换行长文本（行标题/统计行/说明）撑破页面最小宽度"——
    930px 窗口下总览数字卡被挤出可视区（实机截图）+ 逐层 min 追踪定位。
    规则：Body/Caption/StrongBody 类 + 文本 > 16 字符 + 非换行 + 非标题 →
    Ignored（空间充足时零视觉影响；不足时按可用宽压缩，全文保留在 tooltip/原文处）。
    每次数据刷新（apply）后对全页巡检。wordWrap 的标签由其自理，不动。"""
    if root_widget is None:
        return
    for lb in root_widget.findChildren(QLabel):
        try:
            if lb.wordWrap() or isinstance(lb, (TitleLabel, SubtitleLabel)):
                continue
            if lb.property("keepWidth"):
                # 行内自适应省略标签自管宽度（已按 maxWidth 限宽，不会撑破页面；
                # 设 Ignored 会被行内 stretch 项挤到 0 宽——2026-10-11 总览项目名 bug）
                continue
            if lb.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored:
                continue
            if not isinstance(lb, (BodyLabel, CaptionLabel, StrongBodyLabel)):
                continue
            if len(lb.text() or "") <= 16:
                continue
            lb.setSizePolicy(QSizePolicy.Ignored, lb.sizePolicy().verticalPolicy())
        except Exception:  # noqa: BLE001
            pass


def tight_rows(listw, h: int = 36):
    """列表降噪（批次 3D）：行高统一 36px（Fallback 规格：数据列表行高 36px 级、行是线不是卡）。
    在列表填充完成后调用；只收紧已有项，不干预后续 addItem。"""
    try:
        for i in range(listw.count()):
            it = listw.item(i)
            if it is not None and it.sizeHint().height() < h:
                it.setSizeHint(QSize(0, h))
    except Exception:  # noqa: BLE001
        pass


# 徽章色统一走 tokens 单一来源（UI 重构·批次1 2026-10-10）：
# 修正 dsh/claude 白字不达标色、补 workbuddy（原表缺失导致落灰）——详见 tokens.py 验算注释
AGENT_BADGE_COLOR = tokens.AGENT_BADGE

# 退出时未结束线程的全局引用（closeEvent 挂入）：切断 Qt 父子链后由它持住对象，
# 进程经 os._exit 硬杀不触发析构——否则 "QThread: Destroyed while thread is still
# running" abort 即用户报的"有时候退出会报错"（关闭时有耗时线程在跑，间歇性）
_DETACHED_THREADS: list = []
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


class ElidedLabel(CaptionLabel):
    """按实际宽度右端省略（…）的小字标签（继承 CaptionLabel：样式/主题自动跟随）。

    解决「窄容器里长文本生硬断裂/被完全挤没」：显示层自适应省略、原文保留在 fullText()，
    tooltip 由使用方决定。sizeHint 恒按全文宽度申报——防布局拿省略后文本反复收缩。
    与 flex_label 组合使用（flex_label 防撑宽、本类负责裁得好看）。"""

    def __init__(self, text: str = "", parent=None):
        # 注意：不把 text 传给 super().__init__——qfw label 的 str 分派分支内部会再调
        # self.__init__(parent)，子类覆盖 __init__ 时双参调用会递归/参数错配（2026-10-11 实锤）。
        # 统一走 parent 分支 + setText；非 str 第一参数视为 qfw 再入形态（第一参数实为 parent）。
        if not isinstance(text, str):
            text, parent = "", text
        self._full = ""
        super().__init__(parent)
        self.setText(text)

    def setText(self, text: str):
        self._full = text or ""
        self._relayout()

    def fullText(self) -> str:
        return self._full

    def sizeHint(self):
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self._full) + 4, super().sizeHint().height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def _relayout(self):
        w = self.width()
        if w <= 12:  # 尚未布局（宽 0）→ 原样，等 resize 收敛
            if QLabel.text(self) != self._full:
                QLabel.setText(self, self._full)
            return
        fm = self.fontMetrics()
        # 放得下就全文（elidedText 在「额度=文本宽」边界处仍会截——显式判断避开，2026-10-11）
        txt = self._full if fm.horizontalAdvance(self._full) <= w - 2 else \
            fm.elidedText(self._full, Qt.ElideRight, w - 2)
        if QLabel.text(self) != txt:
            QLabel.setText(self, txt)


class ClickBodyLabel(BodyLabel):
    """可点击正文标签；长文本按实际宽度自适应省略（右端 …，原文保留在 fullText()）。
    用于行内长标题：既有省略号体感，又不撑宽（配合 flex_label 使用）。"""
    clicked = Signal()

    def __init__(self, text: str = "", parent=None):
        # 同 ElidedLabel：不走 qfw 的 str 分派（其内部 self.__init__(parent) 再入会与
        # 子类覆盖的 __init__ 参数错配）；非 str 第一参数视为再入形态（实为 parent）
        if not isinstance(text, str):
            text, parent = "", text
        self._full = ""
        super().__init__(parent)
        self.setText(text)

    def setText(self, text: str):
        self._full = text or ""
        self._relayout()

    def fullText(self) -> str:
        return self._full

    def sizeHint(self):
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self._full) + 4, super().sizeHint().height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def _relayout(self):
        w = self.width()
        if w <= 12:  # 尚未布局（宽 0）→ 原样，等 resize 收敛
            if QLabel.text(self) != self._full:
                QLabel.setText(self, self._full)
            return
        fm = self.fontMetrics()
        # 放得下就全文（同 ElidedLabel：避开 elidedText 的等宽边界截断）
        txt = self._full if fm.horizontalAdvance(self._full) <= w - 2 else \
            fm.elidedText(self._full, Qt.ElideRight, w - 2)
        if QLabel.text(self) != txt:
            QLabel.setText(self, txt)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class RecordDetailDialog(MessageBoxBase):
    """时间线记录详情：标题 + 元信息 + 段正文渲染（接收 DB records 行 dict）。
    P1-3：附「引用 / 被引用」区（引用图数据源）。"""

    def __init__(self, win, r: dict):
        super().__init__(win)
        self.titleLabel = SubtitleLabel((r.get("title") or "(无标题)")[:60])
        meta = QHBoxLayout()
        meta.addWidget(badge(r.get("agent", ""), ""))
        rt = (r.get("record_type") or "").strip()
        if rt in brain.RECORD_TYPE_CN:  # P1-2 类型收口的可观测面
            lb = QLabel(f"类型 {brain.RECORD_TYPE_CN[rt]}")
            lb.setStyleSheet(
                f"color:{tokens.C(tokens.ACCENT)};border:1px solid {tokens.C(tokens.STROKE)};"
                "border-radius:8px;padding:1px 8px;font-size:11px;")
            lb.setFixedHeight(20)
            meta.addWidget(lb)
        meta.addWidget(CaptionLabel(f"{r.get('date') or '日期未标注'} · {r.get('project', '')} · 记录#{r.get('id', '?')}"))
        meta.addStretch(1)
        self.browser = TextBrowser()
        self.browser.setHtml(md_to_html(r.get("content") or r.get("title") or ""))
        self.browser.setMinimumSize(860, 400)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addLayout(meta)
        self.viewLayout.addWidget(self.browser)
        # P1-3 引用区：本条引用谁（出链）+ 谁引用本条（反链）
        try:
            rid = int(r.get("id") or 0)
            outs = brain.relation_list(win.root, from_kind="record", from_id=rid, limit=30)
            backs = brain.backlinks(win.root, "record", str(rid), limit=30)
        except Exception:
            outs, backs = [], []
        if outs or backs:
            linksRow = QHBoxLayout()
            outBox, backBox = QVBoxLayout(), QVBoxLayout()
            outBox.addWidget(CaptionLabel(f"→ 本条引用（{len(outs)}）"))
            backBox.addWidget(CaptionLabel(f"← 引用了本条（{len(backs)}）"))
            ol, bl = ListWidget(), ListWidget()
            for x in outs:
                ol.addItem(_rel_text(x["to_kind"], x["to_ref"]))
            for x in backs:
                bl.addItem(f"{_rel_text(x['from_kind'], str(x['from_id']))} · {x['snippet']}")
            if not outs:
                ol.addItem("（无）")
            if not backs:
                bl.addItem("（无）")
            ol.setFixedHeight(110)
            bl.setFixedHeight(110)
            outBox.addWidget(ol)
            backBox.addWidget(bl)
            linksRow.addLayout(outBox, 1)
            linksRow.addLayout(backBox, 1)
            self.viewLayout.addLayout(linksRow)
        openBtn = PushButton(ic("FOLDER", "INFO"), "打开项目目录")
        openBtn.clicked.connect(lambda: open_location(str(Path(win.root) / r.get("project", "")), select=False))
        self.viewLayout.addWidget(openBtn, 0, Qt.AlignRight)
        self.yesButton.setText("关闭")
        self.cancelButton.hide()
        self.widget.setMinimumWidth(920)


def _rel_text(kind: str, ref: str) -> str:
    """关系目标的可读文本（项目名 / 记录#id / 记忆#id / 随记#id）。"""
    if kind == "project":
        return f"项目「{ref}」"
    return f"{ {'record': '记录', 'memory': '记忆', 'note': '随记'}.get(kind, kind) }#{ref}"


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


KIND_BADGE_COLOR = tokens.KIND_BADGE


def kind_badge(kind: str) -> QLabel:
    """记忆类型徽章：唤起排行/置顶记忆的正文行用小字灰文本看不清，类型先入彩色徽章分层。"""
    lb = QLabel(brain.KIND_CN.get(kind, kind))
    lb.setStyleSheet(
        f"color:white;background:{KIND_BADGE_COLOR.get(kind, tokens.BADGE_FALLBACK)};"
        "border-radius:8px;padding:1px 8px;font-size:11px;")
    lb.setFixedHeight(20)
    return lb


def agent_disp(agent: str) -> str:
    """流水/待办行里的 agent 显示名。空/unknown（无主语义）→"未知来源"，
    其余走注册表取规范显示名（2026-10-10：原来直接回显原名，workbuddy 会显示小写）。"""
    a = (agent or "").strip().lower()
    if a in ("", "unknown"):
        return "未知来源"
    return AGENT_CN.get(a) or a


def badge(agent: str, raw: str) -> QLabel:
    # 显示名走**注册表**（2026-10-10：此前硬编码表漏了 workbuddy，
    # 新接入 agent 一律显示小写原名/未标注）。注册表给的是适配器里的规范名。
    a = (agent or "").strip().lower()
    if not a:
        text = raw or "未知来源"  # 确实无归属：诚实标未知，不伪造名字
    else:
        text = AGENT_CN.get(a) or raw or a
    lb = QLabel(text)
    lb.setStyleSheet(
        f"color:white;background:{AGENT_BADGE_COLOR.get(a, tokens.BADGE_FALLBACK)};"
        "border-radius:8px;padding:1px 8px;font-size:11px;")
    lb.setFixedHeight(20)
    return lb


MOOD_COLOR = tokens.MOOD_BADGE  # 心情徽章配色（Daily_You 式元信息呈现，随记页用）


def mood_badge(mood: str) -> QLabel:
    lb = QLabel(mood)
    lb.setStyleSheet(
        f"color:white;background:{MOOD_COLOR.get(mood, tokens.BADGE_FALLBACK)};"
        "border-radius:8px;padding:1px 8px;font-size:11px;")
    lb.setFixedHeight(20)
    return lb


def status_chip(text: str, color_key: str = "text_secondary") -> QLabel:
    """状态胶囊（P2-3c 组件预览 / P3-2 待办状态视图共用）：
    淡底 + 描边 + 语义色文字的小圆角签，随主题自适应。color_key 取 tokens 语义名。"""
    col = tokens.resolve(color_key) or tokens.C(tokens.TEXT_2)
    lb = QLabel(text)
    lb.setStyleSheet(
        f"color:{col};background:{tokens.C('bg_inset')};"
        f"border:1px solid {tokens.C('stroke_default')};border-radius:9px;"
        "padding:1px 8px;font-size:11px;")
    lb.setFixedHeight(20)
    return lb


def rel_time(ts: str) -> str:
    """memos 式相对时间：刚刚/N 分钟前/今天 HH:MM/昨天 HH:MM/MM-DD HH:MM。"""
    try:
        t = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return ts or ""
    now = datetime.now()
    days = (now.date() - t.date()).days
    if days == 0:
        dm = now - t
        if dm.total_seconds() < 60:
            return "刚刚"
        if dm.total_seconds() < 3600:
            return f"{int(dm.total_seconds() // 60)} 分钟前"
        if dm.total_seconds() < 43200:
            return f"{int(dm.total_seconds() // 3600)} 小时前"
        return f"今天 {t:%H:%M}"
    if days == 1:
        return f"昨天 {t:%H:%M}"
    if t.year == now.year:
        return f"{t:%m-%d %H:%M}"
    return f"{t:%Y-%m-%d %H:%M}"


def list_empty_guard(listw, layout, title: str, desc: str = "", index=None):
    """给列表挂空态引导：为空时显示、非空时隐藏（懒创建，挂一次即可）。

    Round 5（2026-10-13）：v2.25 立了「空态组件化」标准，但后续新增/合并的页面没跟上——
    实测空库下有 **6 个页面**的列表是空的却没有任何引导（治理中心 4 个最严重），
    用户首次进入就是一片空白、不知道该干什么。此助手把「显隐 + 懒创建」收在一处，
    页面只需在 reload() 末尾调一次。

    用法：`list_empty_guard(self.listw, self.layout(), "暂无隔离条目", "…")`
    index：插到 layout 的第几位（默认列表后一位；列表不在该 layout 时用，如 QSplitter 场景）
    """
    key = "_empty_hint_widget"
    hint = getattr(listw, key, None)
    empty = (listw.count() == 0)
    if hint is None:
        hint = empty_state("INFO", title, desc)
        try:
            setattr(listw, key, hint)
        except Exception:  # noqa: BLE001  极少数 Qt 对象不允许挂属性：退化为只提示一次
            pass
        if index is None:
            idx = layout.indexOf(listw) if layout is not None else -1
            index = (idx + 1) if idx >= 0 else (layout.count() if layout is not None else 0)
        if layout is not None:
            layout.insertWidget(index, hint)
    hint.setVisible(empty)
    return hint


class MoodPicker(QWidget):
    """心情单选点排（Daily_You 式）：点选高亮为心情色，再点取消——替代下拉框，少一次点击。"""

    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        self._buttons = {}
        for m in [x for x in brain._NOTE_MOODS if x]:
            b = QPushButton(m)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(26)
            b.clicked.connect(lambda _, mm=m: self._pick(mm))
            self._style(b, False)
            h.addWidget(b)
            self._buttons[m] = b
        h.addStretch(1)

    def _style(self, b, on):
        if on:
            b.setStyleSheet(f"color:white;background:{MOOD_COLOR[b.text()]};"
                            "border:none;border-radius:4px;padding:2px 10px;font-size:12px;")
        else:
            b.setStyleSheet(f"color:{tokens.C(tokens.TEXT_2)};background:transparent;"
                            f"border:1px solid {tokens.C(tokens.STROKE)};border-radius:4px;padding:2px 10px;font-size:12px;")

    def _pick(self, m):
        # clicked 信号发出时 Qt 已翻转 checkable 按钮的选中态——直接读当前态，
        # 不可再取反（取反会导致永远选不上，2026-10-07 用户实测"心情选不了"）
        on = self._buttons[m].isChecked()
        for k, b in self._buttons.items():
            b.setChecked(k == m and on)
            self._style(b, k == m and on)
        self.changed.emit(m if on else "")

    def mood(self):
        for m, b in self._buttons.items():
            if b.isChecked():
                return m
        return ""

    def reset(self):
        for b in self._buttons.values():
            b.setChecked(False)
            self._style(b, False)


_WM_HOTKEY, _MOD_CONTROL, _MOD_ALT, _VK_N = 0x0312, 0x2, 0x1, 0x4E


class _HotkeyFilter(QAbstractNativeEventFilter):
    """Windows 全局热键消息过滤器：WM_HOTKEY(id=1) → 呼出随记捕捉小窗。"""

    def __init__(self, cb):
        super().__init__()
        self.cb = cb

    def nativeEventFilter(self, event_type, message):
        if event_type == b"windows_generic_MSG":
            import ctypes.wintypes as wt
            msg = wt.MSG.from_address(int(message))
            if msg.message == _WM_HOTKEY and msg.wParam == 1:
                self.cb()
        return False, 0


class QuickNotePopup(QDialog):
    """全局热键呼出的随记捕捉小窗（Thino Global Capture 式）：热键 → 输入 → Enter 落盘即关。"""

    _instance = None

    @classmethod
    def popup(cls, win):
        if cls._instance is not None and cls._instance.isVisible():
            cls._instance.activateWindow()
            cls._instance.raise_()
            return
        dlg = cls(win)
        cls._instance = dlg
        dlg.show()
        dlg.input.setFocus()

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("随记 · 快速捕捉（Ctrl+Alt+N）")
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.resize(560, 190)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 12)
        v.setSpacing(8)
        self.input = TextEdit()
        self.input.setPlaceholderText("随手记一条…\nEnter 保存并关闭 · Shift+Enter 换行 · Esc 取消")
        self.input.installEventFilter(self)
        v.addWidget(self.input, 1)
        h = QHBoxLayout()
        self.moodPicker = MoodPicker()
        h.addWidget(self.moodPicker)
        h.addStretch(1)
        self.saveBtn = PrimaryPushButton(ic("SEND", "INFO"), "记下")
        self.saveBtn.setEnabled(False)  # 空内容灰禁用（memos 模式），省一次无效点击
        self.saveBtn.clicked.connect(self._submit)
        h.addWidget(self.saveBtn)
        v.addLayout(h)
        self.input.textChanged.connect(
            lambda: self.saveBtn.setEnabled(bool(self.input.toPlainText().strip())))
        self.setStyleSheet(f"QDialog{{background:{tokens.C(tokens.BG_L2)};}}")

    def eventFilter(self, obj, ev):
        if obj is self.input and ev.type() == QEvent.KeyPress:
            if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and not (ev.modifiers() & Qt.ShiftModifier):
                self._submit()
                return True
        return super().eventFilter(obj, ev)

    def _submit(self):
        content = self.input.toPlainText().strip()
        if not content:
            return
        try:
            ctx = brain.active_context(self.win.root)
        except Exception:
            ctx = ""
        try:
            nid = brain.add_note(self.win.root, content, mood=self.moodPicker.mood(),
                                 source="human", context=ctx)
        except Exception as e:  # noqa: BLE001
            InfoBar.error("随记保存失败", str(e), duration=5000, parent=self.win)
            return
        self.close()
        try:
            self.win.notes_page.reload()
        except Exception:
            pass
        InfoBar.success("已记入随记", f"#{nid}——思想已进大脑，所有 agent 检索可见",
                        duration=3000, parent=self.win)


def clear_layout(lay):  # 递归摘净控件与子布局（随记时间线重绘用；行布局不拆净会残留叠加）
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget()
        if w:
            w.setParent(None)
            w.deleteLater()
        sub = it.layout()
        if sub:
            clear_layout(sub)
            sub.deleteLater()


class NumberCard(CardWidget):
    """可点击指标卡：只给绑了 clicked 的实例用（卡片=可交互区域的 Fluent 语义），
    纯展示读数直接用 TitleLabel+CaptionLabel 排版，不再包卡。"""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
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
        self.hint.setStyleSheet(f"color:{tokens.C(tokens.DANGER)};" if err else f"color:{tokens.C(tokens.SUCCESS)};")
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
        try:  # P1-2 标签收口：已有标签注册表做输入补全（fail-open，取不到就不补全）
            _tags = [t["tag"] for t in brain.tag_list(win.root, 200)] if getattr(win, "root", "") else []
            if _tags:
                _comp = QCompleter(_tags, self.tagsEdit)
                _comp.setCaseSensitivity(Qt.CaseInsensitive)
                _comp.setFilterMode(Qt.MatchContains)
                self.tagsEdit.setCompleter(_comp)
        except Exception:
            pass
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


class MemoryHistoryDialog(MessageBoxBase):
    """P1-1 版本历史：修订列表（谁/何时/改了啥）+ 内容 diff + 一键回滚。
    回滚本身也写入一条修订（可再回滚）——版本链完整、只增不改。"""

    _FIELDS_CN = {"content": "内容", "kind": "类型", "tags": "标签", "pinned": "置顶", "status": "状态"}

    def __init__(self, win, mid: int):
        super().__init__(win)
        self.win = win
        self.mid = mid
        self._sel_rev = None
        self.titleLabel = SubtitleLabel(f"版本历史 · 记忆 #{mid}")
        self.revList = ListWidget()
        self.revList.setFixedWidth(300)
        self.detail = TextBrowser()
        row = QHBoxLayout()
        row.addWidget(self.revList)
        row.addWidget(self.detail, 1)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addLayout(row)
        self.yesButton.setText("回滚到此版本")
        self.cancelButton.setText("关闭")
        self.widget.setMinimumSize(880, 480)
        self.revList.itemClicked.connect(self._on_pick)
        self._reload()

    def _reload(self):
        self.revList.clear()
        self._sel_rev = None
        revs = brain.memory_revisions(self.win.root, self.mid)
        if not revs:
            self.revList.addItem("（暂无修订——编辑/删除/回滚后才有）")
            self.detail.setHtml(
                f"<p style='color:{tokens.C(tokens.TEXT_3)}'>该记忆还没有修订历史。"
                "修改内容后回来，这里会有每一次改动的快照与 diff。</p>")
            return
        for r in revs:
            self.revList.addItem(
                f"{r['created'][5:16].replace('T', ' ')} · {r['action']} · {r['agent'] or '-'}")
            self.revList.item(self.revList.count() - 1).setData(Qt.UserRole, r["id"])
        self.detail.setHtml(
            f"<p style='color:{tokens.C(tokens.TEXT_3)}'>点左侧某条修订查看改动详情；"
            "选好后点『回滚到此版本』。</p>")

    def _on_pick(self, item):
        rid = item.data(Qt.UserRole)
        if rid is None:
            return
        rev = next((r for r in brain.memory_revisions(self.win.root, self.mid) if r["id"] == rid), None)
        if rev:
            self._sel_rev = rev
            self.detail.setHtml(self._render(rev))

    def _render(self, rev) -> str:
        import difflib
        try:
            b = json.loads(rev["before"] or "{}")
            a = json.loads(rev["after"] or "{}")
        except Exception:
            return "<p>快照损坏，无法展示</p>"
        head = (f"<p><b>修订 #{rev['id']}</b> · {html.escape(rev['action'])} · "
                f"{html.escape(rev['agent'] or '-')} · {html.escape(rev['created'])}</p>")
        if rev["action"] == "delete":
            return head + ("<p>删除前内容（点『回滚到此版本』可复活）：</p>"
                           f"<pre>{html.escape(str(b.get('content', '')))}</pre>")
        parts = [head]
        changed = [k for k in ("content", "kind", "tags", "pinned", "status") if b.get(k) != a.get(k)]
        if "content" in changed:
            diff = list(difflib.unified_diff(
                str(b.get("content", "")).splitlines(), str(a.get("content", "")).splitlines(),
                lineterm="", n=1))
            body = []
            for ln in diff:
                e = html.escape(ln)
                if ln.startswith("+"):
                    body.append(f"<span style='color:{tokens.C(tokens.SUCCESS)}'>{e}</span>")
                elif ln.startswith("-"):
                    body.append(f"<span style='color:{tokens.C(tokens.DANGER)}'>{e}</span>")
                else:
                    body.append(e)
            parts.append("<p>内容 diff：</p><pre>" + "\n".join(body) + "</pre>")
        for k in changed:
            if k == "content":
                continue
            parts.append(f"<p>{self._FIELDS_CN.get(k, k)}：{html.escape(str(b.get(k)))} → "
                         f"{html.escape(str(a.get(k)))}</p>")
        if not changed:
            parts.append("<p>（无字段差异）</p>")
        return "".join(parts)

    def accept(self):
        """『回滚到此版本』：确认后回滚并原地刷新（不关窗，新修订立即可见）。"""
        if not self._sel_rev:
            InfoBar.warning("先选一条修订", "", duration=2000, parent=self.win)
            return
        rev = self._sel_rev
        box = MessageBox("确认回滚？",
                         f"将记忆 #{self.mid} 回滚到修订 #{rev['id']} 之前的状态；"
                         "回滚本身也会写入一条修订（可再回滚）。", self.win)
        if not box.exec():
            return
        err = brain.memory_rollback(self.win.root, self.mid, rev["id"], agent="user")
        if err:
            InfoBar.error("回滚失败", err, duration=4000, parent=self.win)
            return
        core.journal(self.win.root, "user", "memory_rollback", f"brain:memories#{self.mid}",
                     note=f"rev#{rev['id']}")
        InfoBar.success("已回滚", f"记忆 #{self.mid} → 修订 #{rev['id']} 之前", duration=2500,
                        parent=self.win)
        self._reload()


def _agent_cn() -> dict:
    """agent id → 显示名。**由core 注册表派生**（2026-10-09）。

    过去是硬编码 AGENT_CN，漏了 workbuddy → 时间线徽章显示小写 workbuddy 而非 WorkBuddy。
    现在改从 core.agent_known_names() 取，新增 agent 零改码；取不到时退回旧硬编码表。"""
    try:
        reg = core.agent_known_names(core.get_root())
        #注册表给的是适配器里的原名（"Claude Code"/"ZCode"），直接用作显示名
        return {k: v for k, v in reg.items() if v}
    except Exception:  # noqa: BLE001
        return {"zcode": "ZCode", "hermes": "hermes", "deepseek": "deepseek",
                "claude": "claude", "codex": "codex", "dsh": "DSH", "其他": "其他"}


AGENT_CN = _agent_cn()  # 模块级快照（导入时求值一次；运行期刷新见refresh_agent_cn）
# 显示名（小写）→ 库内 id 的反查表（过滤用：下拉给显示名，库里存 id）
_AGENT_DISP2ID = {v.strip().lower(): k for k, v in AGENT_CN.items() if v}


def refresh_agent_cn() -> dict:
    """重取注册表并刷新 AGENT_CN（大脑根切换/新增 agent 后调用）。返回新表。"""
    global AGENT_CN, _AGENT_DISP2ID
    AGENT_CN = _agent_cn()
    _AGENT_DISP2ID = {v.strip().lower(): k for k, v in AGENT_CN.items() if v}
    return AGENT_CN


# 再导出白名单：含单下划线名（_page_head/_display_title/... 页面与窗口要用）
__all__ = [n for n in list(globals()) if not n.startswith("__")]
