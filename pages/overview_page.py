# -*- coding: utf-8 -*-
"""OverviewPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
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

        # 数字卡区：FlowLayout —— 窄窗自动 4→2→1 列换行（原 QHBoxLayout 只会等比压扁）
        row = FlowLayout()
        self.cardProj = NumberCard("项目总数", icon_name="FOLDER")
        self.cardRec = NumberCard("工作记录条数", icon_name="HISTORY")
        self.cardToday = NumberCard("今日动态", icon_name="CALENDAR")
        self.cardTodo = NumberCard("待处理（Inbox+对账）", icon_name="FLAG")
        for c in (self.cardProj, self.cardRec, self.cardToday, self.cardTodo):
            row.addWidget(c)
        lay.addLayout(row)
        # 数字卡点击直达对应页（CardWidget 自带 clicked）
        self.cardProj.clicked.connect(lambda: self.win.switchTo(self.win.project_page))
        self.cardRec.clicked.connect(lambda: self.win.switchTo(self.win.timeline_page))
        self.cardToday.clicked.connect(lambda: self.win.switchTo(self.win.timeline_page))
        self.cardTodo.clicked.connect(self.goto_todo)

        # 内容组不包卡：间距+小标题分组；NumberCard 保留框（绑了 clicked，卡片=可点区域）
        cols = QHBoxLayout()
        cols.setSpacing(24)  # 左右两栏列距 > 行距，分组靠间距
        # 左：最近 agent 动态
        recentCol = QVBoxLayout()
        recentCol.addWidget(section_head("最近动态（点击看详情）", "HISTORY"))
        self.recentBox = QVBoxLayout()
        self.recentBox.setSpacing(4)
        recentCol.addLayout(self.recentBox)
        recentCol.addStretch(1)
        cols.addLayout(recentCol, 3)
        # 右：agent 阵容 + 快捷入口
        rightCol = QVBoxLayout()
        rightCol.setSpacing(8)
        rightCol.addWidget(section_head("Agent 阵容（Agent 中心探测）", "ROBOT"))
        self.agentBox = QVBoxLayout()
        self.agentBox.setSpacing(4)
        rightCol.addLayout(self.agentBox)
        rightCol.addSpacing(16)
        rightCol.addWidget(section_head("最近会话（hub_heartbeat 上报 · 近 24 小时）", "PEOPLE"))
        self.sessionBox = QVBoxLayout()
        self.sessionBox.setSpacing(4)
        rightCol.addLayout(self.sessionBox)
        rightCol.addSpacing(16)
        rightCol.addWidget(section_head("快捷入口", "MENU"))
        quickRow = QHBoxLayout()
        for text, target in (("打开时间线", "timeline"), ("打开 Agent 中心", "hub"),
                             ("检查对账问题", "audit"), ("搜索全部记录", "search")):
            b = PushButton(text)
            b.clicked.connect(lambda _, t=target: self.goto(t))
            quickRow.addWidget(b)
        quickRow.addStretch(1)
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
            self.sessionBox.addWidget(empty_state(
                "PEOPLE", "近 24 小时暂无会话", "agent 开工调 hub_heartbeat 后显示"))
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

