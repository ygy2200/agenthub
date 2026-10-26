# -*- coding: utf-8 -*-
"""OverviewPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）


class _CardGrid(QLayout):
    """指标卡等宽网格：4/190px 宽、同列等宽，按可用宽度 4→2→1 列。

    FlowLayout 只按卡片 sizeHint 决定换行，920 视口下会排成 3+1、右侧留空一截；
    这里按行等分可用宽度（列宽一致、右缘对齐），并保持 minimumSize 只有单卡宽，
    不把 4 列的最小宽度强加给整页（否则窄窗会出现横向滚动）。
    """

    def __init__(self, parent=None, gap=None, min_w=190):
        super().__init__(parent)
        self._items: list = []
        self._gap = tokens.gap(tokens.GAP_CARD) if gap is None else gap
        self._min_w = min_w
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._grid(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._grid(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def columns(self, width: int) -> int:
        if width >= 4 * self._min_w + 3 * self._gap:
            return 4
        if width >= 2 * self._min_w + self._gap:
            return 2
        return 1

    def _grid(self, rect, test_only: bool) -> int:
        items = [it for it in self._items
                 if it.widget() is not None and it.widget().isVisible()]
        if not items:
            return 0
        cols = self.columns(rect.width())
        cw = max(self._min_w, (rect.width() - self._gap * (cols - 1)) // cols)
        y, row_h = rect.y(), 0
        for i, it in enumerate(items):
            r, c = divmod(i, cols)
            if c == 0 and r:
                y += row_h + self._gap
                row_h = 0
            h = it.sizeHint().height()
            if not test_only:
                it.setGeometry(QRect(rect.x() + c * (cw + self._gap), y, cw, h))
            row_h = max(row_h, h)
        return y + row_h - rect.y()


class OverviewPage(QWidget):
    """总览首页：打开软件第一眼看到全局——今天谁在干活、有多少待处理、agent 阵容。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.recent: list = []
        self.snap = None
        # 外层滚动（#2055 修复模式）：内容超高时滚动，永不压缩成重叠细条
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.enableTransparentBackground()
        _body = QWidget()
        self.scroll.setWidget(_body)
        self.scroll.enableTransparentBackground()  # 必须在 setWidget 后（qfw API 需 widget 已存在才命中 body；2026-10-10 深底错配根因）
        outer.addWidget(self.scroll)
        lay = QVBoxLayout(_body)
        set_page_margins(lay)

        head = QHBoxLayout()
        self.hello = PageTitleLabel("总览")   # 页面标题统一 24px（不再与数字卡数值同字号）
        head.addWidget(self.hello)
        head.addStretch(1)
        self.quickNew = PrimaryPushButton(ic("ADD", "INFO"), "新建项目")
        self.quickNew.clicked.connect(lambda: self.win.project_page.new_project())
        head.addWidget(self.quickNew)
        lay.addLayout(head)
        _ovDesc = CaptionLabel("电脑上所有 agent 的工作入口，数据实时来自目录扫描（F5 刷新）")
        _ovDesc.setWordWrap(True)
        lay.addWidget(_ovDesc)

        # 随记输入条（08 计划 A-Ⅰ：人的高频入口放首页=最短路径；完整视图在「随记」页）
        lay.addSpacing(16)
        lay.addWidget(section_head("随记（你的顿悟与想法——回车即存，所有 agent 检索可见）", "EDIT"))
        noteRow = QHBoxLayout()
        self.noteInput = LineEdit()
        self.noteInput.setPlaceholderText("随手记一条想法/顿悟/摘录…（回车即存）")
        self.noteInput.setClearButtonEnabled(True)
        self.noteInput.returnPressed.connect(self.save_note)
        noteRow.addWidget(self.noteInput, 1)
        self.moodPicker = MoodPicker()
        noteRow.addWidget(self.moodPicker)
        self.noteBtn = PrimaryPushButton(ic("SEND", "INFO"), "记下")
        self.noteBtn.setEnabled(False)  # 空内容灰禁用（memos 模式）
        self.noteBtn.clicked.connect(self.save_note)
        noteRow.addWidget(self.noteBtn)
        lay.addLayout(noteRow)
        self.noteInput.textChanged.connect(
            lambda: self.noteBtn.setEnabled(bool(self.noteInput.text().strip())))

        # 数字卡区：等宽网格 —— 920 视口 4 卡同排，更窄时 2/1 列（FlowLayout 会排成 3+1）
        self.cardProj = NumberCard("项目总数", icon_name="FOLDER")
        self.cardRec = NumberCard("工作记录条数", icon_name="HISTORY")
        self.cardToday = NumberCard("今日动态", icon_name="CALENDAR")
        self.cardTodo = NumberCard("待处理（Inbox+对账）", icon_name="FLAG")
        self.cardGrid = _CardGrid()
        for c in (self.cardProj, self.cardRec, self.cardToday, self.cardTodo):
            c.setMinimumWidth(190)  # 823px 内容宽下四卡各约 196px（实测 195px 标题仍完整）
            self.cardGrid.addWidget(c)
        lay.addLayout(self.cardGrid)
        # 数字卡点击直达对应页（CardWidget 自带 clicked）
        self.cardProj.clicked.connect(lambda: self.win.switchTo(self.win.project_page))
        self.cardRec.clicked.connect(lambda: self.win.switchTo(self.win.timeline_page))
        self.cardToday.clicked.connect(lambda: self.win.switchTo(self.win.timeline_page))
        self.cardTodo.clicked.connect(self.goto_todo)

        # 内容组不包卡：间距+小标题分组；NumberCard 保留框（绑了 clicked，卡片=可点区域）
        cols = QHBoxLayout()
        cols.setSpacing(tokens.SPACE_2XL)  # 左右两栏列距 > 行距，分组靠间距
        # 左：最近 agent 动态
        recentCol = QVBoxLayout()
        recentCol.addWidget(section_head("最近动态（点击看详情）", "HISTORY"))
        self.recentBox = QVBoxLayout()
        self.recentBox.setSpacing(tokens.SPACE_XS)
        recentCol.addLayout(self.recentBox)
        recentCol.addStretch(1)
        cols.addLayout(recentCol, 3)
        # 右：agent 阵容 + 快捷入口
        rightCol = QVBoxLayout()
        rightCol.setSpacing(tokens.SPACE_S)
        rightCol.addWidget(section_head("Agent 阵容（Agent 中心探测）", "ROBOT"))
        self.agentBox = QVBoxLayout()
        self.agentBox.setSpacing(tokens.SPACE_XS)
        rightCol.addLayout(self.agentBox)
        rightCol.addSpacing(16)
        rightCol.addWidget(section_head("最近会话（hub_heartbeat 上报 · 近 24 小时）", "PEOPLE"))
        self.sessionBox = QVBoxLayout()
        self.sessionBox.setSpacing(tokens.SPACE_XS)
        rightCol.addLayout(self.sessionBox)
        rightCol.addSpacing(16)
        rightCol.addWidget(section_head("快捷入口", "MENU"))
        quickRow = FlowLayout()  # 窄窗自动换行：4 个按钮并排会把右栏最小宽度顶到 400+
        for text, target in (("打开时间线", "timeline"), ("打开 Agent 中心", "hub"),
                             ("检查对账问题", "audit"), ("搜索全部记录", "search")):
            b = PushButton(text)
            b.clicked.connect(lambda _, t=target: self.goto(t))
            quickRow.addWidget(b)
        rightCol.addLayout(quickRow)
        rightCol.addStretch(1)
        cols.addLayout(rightCol, 2)
        lay.addLayout(cols)
        lay.addStretch(1)

        # 加载态：首屏先放骨架条，数据到达后由 set_* 清空替换
        # （空白列表无法区分"在加载"与"真的没数据"，观感等同于坏了）
        self.recentBox.addWidget(Skeleton(4))
        self.agentBox.addWidget(Skeleton(3))
        self.sessionBox.addWidget(Skeleton(3))

    def goto(self, key):
        page = {"timeline": self.win.timeline_page, "hub": self.win.hub_page,
                "audit": self.win.audit_page, "search": self.win.search_page}[key]
        self.win.switchTo(page)

    def save_note(self):
        """随记保存（08 计划 A-Ⅰ）：回车或点「记下」即入 notes 表，人的思想进大脑。"""
        content = self.noteInput.text().strip()
        if not content:
            return  # 按钮空态已灰禁用，此路径仅防御（回车空内容）
        mood = self.moodPicker.mood()
        try:
            ctx = brain.active_context(self.win.root)
        except Exception:
            ctx = ""
        try:
            nid = brain.add_note(self.win.root, content, mood=mood, source="human", context=ctx)
        except Exception as e:  # noqa: BLE001
            InfoBar.error("随记保存失败", str(e), duration=5000, parent=self.win)
            return
        self.noteInput.clear()
        self.moodPicker.reset()
        self.win.notes_page.reload()  # 随记页时间线跟上
        InfoBar.success("已记入随记", f"#{nid}——思想已进大脑，所有 agent 检索可见",
                        duration=3000, parent=self.win)

    def goto_todo(self):
        """待处理卡直达对账分段（2026-10-01 审计补充：数字卡可点击）。"""
        self.win.ledger_page.seg.setCurrentItem("audit")
        self.win.switchTo(self.win.ledger_page)

    def set_snapshot(self, snap):
        self.snap = snap
        self.cardTodo.value.setText(str(len(snap.issues) + len(snap.inbox)))
        # 上下文行（口径）：总数说不清"待收"和"待改"各多少，拆开才可执行
        self.cardTodo.set_context(f"待收 {len(snap.inbox)} · 待改 {len(snap.issues)}")

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
            _bdg = badge(r.get("agent", ""), "")
            # 徽章列固定宽：否则标题起始 x 逐行不齐（"WorkBuddy" 比 "ZCode" 宽一截）
            _bdg.setFixedWidth(BADGE_COL_W)
            _bdg.setAlignment(Qt.AlignCenter)
            row.addWidget(_bdg)
            t = flex_label(ClickBodyLabel(_display_title(r)))
            t.setToolTip(f"{_display_title(r)}\n点击查看完整记录")
            t.setCursor(Qt.PointingHandCursor)
            t.clicked.connect(lambda _, rr=r: RecordDetailDialog(self.win, rr).exec())
            row.addWidget(t, 1)
            # 项目名：自适应省略 + 宽度上限（原 flex_label 被行内 stretch 挤成 0 宽→完全不可见，
            # 2026-10-11 修复；keepWidth 防 guard_overflow 再设 Ignored）
            pn = ElidedLabel(r.get("project", ""))
            pn.setToolTip(r.get("project", ""))
            pn.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            pn.setFixedWidth(PROJECT_COL_W)     # 固定列宽（原 min110/max220 是猜的）
            pn.setProperty("keepWidth", True)
            row.addWidget(pn)
            wrap = QWidget()
            wrap.setLayout(row)
            wrap.setFixedHeight(tokens.row_height())   # 行高走密实度 token
            attach_hover(wrap, separator=True)         # hover 反馈 + 1px 行分隔线
            self.recentBox.addWidget(wrap)
        if not self.recent:
            self.recentBox.addWidget(empty_state(
                "HISTORY", "暂无记录", "agent 记一笔工作后，最近动态出现在这里"))

    def set_agents(self, agents, reg=None):
        """R132：阵容行改「状态优先」——在线/离线 + 最近项目（大脑注册表），
        技能/MCP/记忆数退到 tooltip（原来三组数字堆在行里，看不出谁在干活）。"""
        reg = reg or {}
        while self.agentBox.count():
            it = self.agentBox.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not agents:
            self.agentBox.addWidget(CaptionLabel(
                "（探测中，或未检测到 agent——去「Agent 中心」点「重新探测」，或手动添加 agent 目录）"))
            return
        for a in agents:
            r = reg.get((a.name or "").lower())
            tip_bits = [f"技能 {len(a.skills)} · MCP {len(a.mcps)} · 记忆 {len(a.memories)}",
                        a.home or ""]
            if r:
                state = "● 在线" if r.get("online") else "○ 离线"
                last_p = (r.get("last_project") or "").strip()
                seen = (r.get("last_seen") or "").strip()
                if seen and "T" in seen:
                    d, _, hm = seen.partition("T")
                    seen = (d[5:] + " " + hm[:5]) if d[:4] == str(__import__("datetime").date.today().year) else d
                main = f"{state} · {a.name}" + (f" —— 最近在「{last_p}」" if last_p else "")
                if seen:
                    main += f"（{seen}）"
            else:
                main = f"{'●' if a.detected else '○'} {a.name}（未接入大脑）"
            # 920×600 实测：不省略的整行会顶高右栏最小宽度（942px > 视口 871），
            # 全页横向溢出、最近动态标题被挤到 0 宽。截断交给布局、全文进 tooltip。
            line = ElidedLabel(main)
            line.setToolTip(" / ".join(b for b in tip_bits if b))
            self.agentBox.addWidget(flex_label(line))

    def set_sessions(self, sessions):
        while self.sessionBox.count():
            it = self.sessionBox.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not sessions:
            self.sessionBox.addWidget(empty_state(
                "PEOPLE", "近 24 小时暂无会话", "agent 开工报到后显示"))
            return
        for s in sessions[:6]:
            when = rel_time(s.get("ts", ""))
            txt = f"● {s.get('agent')} → {s.get('project') or '未指定'}"
            if when:
                txt += f"（{when}）"
            note = s.get("note") or ""
            if note:
                txt += f"  {note}"
            _lb = ElidedLabel(txt)
            _lb.setToolTip(f"{s.get('agent')} → {s.get('project') or '未指定'}\n"
                           f"最后活跃：{str(s.get('ts') or '')[:19]}\n{note}".strip())
            self.sessionBox.addWidget(flex_label(_lb))

