# -*- coding: utf-8 -*-
"""Ctrl+K 命令面板（UI 重构·批次 3A，2026-10-10）。

规格来源（原文已归档 output/学习素材-GitHub案例/）：
  - 课表 app/ui/command_palette.py（129 行，同栈已验收）——弹窗形态 / 动作收集 / 回车执行
  - antigravity-clone CommandPalette.tsx（303 行）——↑↓ 循环 / 无结果空态 / 打开清空+延迟聚焦 / 36px 行高
设计决定：
  - 搜索框恒聚焦，面板级 eventFilter 截 ↑↓ / Enter / Esc——键盘全流程（不依赖列表焦点）
  - 过滤 = label+关键词 子串匹配（中文友好；v1 不做模糊算法，避免误排序）
  - 内容三组前缀：打开：/ 动作：/ 项目：/ 外观：（分组靠前缀，v1 不做分节渲染）
  - 配色全走 tokens；popup 时按当前主题刷新样式（深浅切换后即时正确）
"""
import os

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QAbstractItemView, QListWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, InfoBar, LineEdit, ListWidget

import actions  # P2-2：页面/动作条目与导航、快捷键同源
import tokens


def _ic(name: str, fallback: str = "INFO"):
    """图标枚举兜底（与 ui.ic 同思路），防不同 qfw 版本枚举缺失。"""
    return getattr(FluentIcon, name, getattr(FluentIcon, fallback))


class CommandPalette(QWidget):
    """置顶命令面板：搜索并执行 跳页 / 动作 / 项目直达 / 外观切换。"""

    def __init__(self, window):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("命令面板")
        self.win = window
        self._acts = []  # [(icon_name, label, keywords, fn, group)]

        card = QWidget(self)
        card.setObjectName("cpCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        # 2026-10-16 复盘：原写 (12,12,12,10) + setSpacing(8)——10 不在 4px 网格上，
        # 且 palette.py 不在间距守卫的扫描范围里，所以一直没人发现。统一走 token。
        lay.setContentsMargins(tokens.SPACE_M, tokens.SPACE_M, tokens.SPACE_M, tokens.SPACE_M)
        lay.setSpacing(tokens.SPACE_S)

        self.search = LineEdit(card)
        self.search.setPlaceholderText("搜索：页面 / 动作 / 项目…（↑↓ 选择 · Enter 执行 · Esc 关闭）")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(36)
        self.search.textChanged.connect(self._filter)
        self.search.installEventFilter(self)
        lay.addWidget(self.search)

        self.listw = ListWidget(card)
        self.listw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.listw.setTextElideMode(Qt.ElideRight)
        self.listw.itemClicked.connect(self._run_item)
        self.listw.installEventFilter(self)
        lay.addWidget(self.listw)

        self.hint = CaptionLabel("", card)
        lay.addWidget(self.hint)

        self.setFixedSize(620, 470)

    # ---------------------------------------------------------------- 弹出/收集

    def popup(self):
        """收集动作 → 清空 → 居中上部 → 显示聚焦（antigravity 时序：清空+重置+聚焦）。"""
        self._collect()
        self._restyle()
        self.search.clear()
        self._filter("")
        scr = self.win.screen().availableGeometry()
        self.move(scr.center().x() - self.width() // 2,
                  scr.top() + int(scr.height() * 0.12))
        self.show()
        self.raise_()
        self.activateWindow()
        # 延迟聚焦：等面板渲染完。定时器挂本面板——面板先销毁则这次延后自动作废，
        # 不会在已删除的输入框上跑 setFocus（R239 同批清理的调用点）。
        self._focusTimer = QTimer(self)
        self._focusTimer.setSingleShot(True)
        self._focusTimer.timeout.connect(self.search.setFocus)
        self._focusTimer.start(30)

    def _collect(self):
        """每次 popup 重新收集（数据动态变化：项目列表/主题）。
        P2-2：页面项与固定动作来自 actions 注册表（与左侧导航、Ctrl+1..9 同源，杜绝三处漂移）。"""
        w = self.win
        acts = []
        for _attr, title, icon, kw, page in actions.page_entries(w):
            acts.append((icon, f"打开：{title}", kw, (lambda p=page: w.switchTo(p)), "page"))
        handlers = {
            "refresh": w.refresh,
            "new_note": self._focus_note,
            "gen_report": self._gen_report,
            "open_root": self._open_root,
            "theme_dark": lambda: self._set_theme("dark"),
            "theme_light": lambda: self._set_theme("light"),
            "theme_auto": lambda: self._set_theme("auto"),
        }
        for aid, label, icon, kw in actions.ACTIONS:
            fn = handlers.get(aid)
            if fn is not None:
                acts.append((icon, label, kw, fn, "action"))
        snap = getattr(getattr(w, "project_page", None), "snap", None)
        names = [getattr(p, "name", "") for p in getattr(snap, "projects", []) or []]
        # 大脑里已有记录、但还没建目录的项目也要能直达（项目页列表读 DB，不只是扫描结果）。
        names += [p.get("name", "") for p in
                  getattr(getattr(w, "project_page", None), "db_projects", []) or []]
        seen = set()
        for name in names:
            if not name or name in seen:
                continue
            seen.add(name)
            acts.append(("FOLDER", f"项目：{name}", "proj 打开项目 工作记录",
                         (lambda n=name: self._open_project(n)), "project"))
            if len(seen) >= 60:
                break
        # P3-1：MCP 工具检索（命令面板搜工具）——清单与 MCP server 同源（import 其 TOOLS 常量）
        try:
            import agenthub_mcp
            for t in agenthub_mcp.TOOLS:
                acts.append(("CODE", f"工具：{t['name']}",
                             f"{t['name']} {t['description']}".lower(),
                             (lambda n=t["name"], d=t["description"]: self._use_tool(n, d)),
                             "tool"))
        except Exception:  # noqa: BLE001 工具清单不可用时面板照常
            pass
        self._acts = acts

    # ---------------------------------------------------------------- 样式

    def _restyle(self):
        """按当前主题刷新面板样式（tokens 单一来源）。"""
        self.setStyleSheet(
            f"QWidget#cpCard{{background:{tokens.C(tokens.BG_L2)};"
            f"border:1px solid {tokens.C(tokens.STROKE)};"
            # 2026-10-16 复盘：原硬编码 border-radius:12px（不在 2/4/8 白名单，
            # 也是 palette.py 不在守卫扫描范围内才能一直活着）→ 改走令牌
            f"border-radius:{tokens.RADIUS_CARD}px;}}")
        self.listw.setStyleSheet("ListWidget{background:transparent;border:none;}")
        self.hint.setStyleSheet(f"color:{tokens.C(tokens.TEXT_3)};font-size:12px;")

    # ---------------------------------------------------------------- 过滤/执行

    def _filter(self, text: str):
        text = (text or "").strip().lower()
        terms = [t for t in text.split() if t]
        self.listw.clear()
        n = 0
        for icon, label, kw, fn, group in self._acts:
            # 空输入只列页面与常用动作：项目最多 60 个、MCP 工具 69 个全倒出来会淹没入口。
            if not terms and group not in ("page", "action"):
                continue
            hay = f"{label} {kw}".lower()
            if terms and not all(t in hay for t in terms):
                continue
            it = QListWidgetItem(label)
            try:
                it.setIcon(_ic(icon).icon())
            except Exception:  # noqa: BLE001 图标失败不阻断
                pass
            it.setSizeHint(QSize(0, 36))
            it.setData(Qt.UserRole, (label, fn))
            self.listw.addItem(it)
            n += 1
        if n:
            self.listw.setCurrentRow(0)
        else:
            # 无结果空态（antigravity：不能白屏）；两行：说清没匹配 + 给可试关键词。
            it = QListWidgetItem("无匹配命令——换个关键词试试")
            it.setFlags(Qt.NoItemFlags)
            it.setSizeHint(QSize(0, 36))
            it.setToolTip("输入页面/动作/项目/MCP 工具名")
            self.listw.addItem(it)
            it2 = QListWidgetItem("试试「项目」「随记」「退回」「hub_search」")
            it2.setFlags(Qt.NoItemFlags)
            it2.setSizeHint(QSize(0, 36))
            it2.setToolTip("这些是真实存在的页面、动作与工具名")
            self.listw.addItem(it2)
        if not n:
            self.hint.setText("0 项 · 换个关键词，或 Esc 关闭")
        elif terms:
            self.hint.setText(f"{n} 项 · ↑↓ 选择 · Enter 执行 · Esc 关闭")
        else:
            self.hint.setText(f"{n} 项 · 可再输项目名/工具名 · ↑↓ 选择 · Enter 执行 · Esc 关闭")

    def _move(self, delta: int):
        """↑↓ 循环选择（antigravity 取模），并保证选中项可见。"""
        n = self.listw.count()
        if n <= 0:
            return
        cur = max(self.listw.currentRow(), 0)
        nxt = (cur + delta) % n
        it = self.listw.item(nxt)
        if it is None or not (it.flags() & Qt.ItemIsSelectable):
            return
        self.listw.setCurrentRow(nxt)
        self.listw.scrollToItem(it, QAbstractItemView.EnsureVisible)

    def _run_item(self, it):
        data = it.data(Qt.UserRole)
        if not data:
            return
        _label, fn = data
        if fn is None:
            return
        self.hide()
        try:
            fn()
        except Exception:  # noqa: BLE001 动作失败不崩面板
            pass

    # ---------------------------------------------------------------- 动作实现

    def _focus_note(self):
        w = self.win
        w.switchTo(w.notes_page)
        w.notes_page.noteInput.setFocus()

    def _gen_report(self):
        w = self.win
        w.switchTo(w.report_page)
        w.report_page.generate()

    def _open_root(self):
        w = self.win
        if w.root and os.path.isdir(w.root):
            QDesktopServices.openUrl(QUrl.fromLocalFile(w.root))

    def _set_theme(self, mode: str):
        tokens.apply_theme(mode)
        tokens.save_theme_choice(mode)

    def _open_project(self, name: str):
        w = self.win
        w.switchTo(w.project_page)
        try:
            w.project_page.select_project(name)
        except Exception:  # noqa: BLE001
            pass

    def _use_tool(self, name: str, desc: str):
        """工具条目（P3-1）：复制工具名到剪贴板 + 提示说明——供人工核对/粘贴到 agent 对话。"""
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(name)
        except Exception:  # noqa: BLE001
            pass
        InfoBar.success("已复制工具名", f"{name} —— {desc[:88]}", duration=3500, parent=self.win)

    # ---------------------------------------------------------------- 键盘

    def eventFilter(self, obj, ev):
        """键盘全流程：↑↓ 循环 / Enter 执行 / Esc 关闭（搜索框恒聚焦，不依赖列表焦点）。"""
        if ev.type() == QEvent.KeyPress:
            k = ev.key()
            if k == Qt.Key_Escape:
                self.hide()
                return True
            if k in (Qt.Key_Up, Qt.Key_Down):
                self._move(-1 if k == Qt.Key_Up else 1)
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                it = self.listw.currentItem()
                if it is not None:
                    self._run_item(it)
                return True
        return super().eventFilter(obj, ev)
