# -*- coding: utf-8 -*-
"""NotesPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
class NotesPage(QWidget):
    """随记页（08 方向 A：人的模块容器）——memos 式轻捕获 + 创意层：
    心情热力图 / 实时关联浮出（Smart Connections 式）/ 不期而遇（flomo 式每日回顾）/
    时光回放（On This Day）/ 时光胶囊 / 收藏重温 / #标签内联与主题串 / 草稿自动保存。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self._tag_filter = ""
        self._relate_timer = QTimer(self)  # 实时关联防抖：输入停顿 800ms 再检索
        self._relate_timer.setSingleShot(True)
        self._relate_timer.setInterval(800)
        self._relate_timer.timeout.connect(self._show_related)
        self._draft_timer = QTimer(self)   # 草稿自动保存防抖
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(1200)
        self._draft_timer.timeout.connect(self._save_draft)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = ScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.enableTransparentBackground()
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 24, 24, 24)

        head = QHBoxLayout()
        head.addWidget(TitleLabel("随记"))
        head.addStretch(1)
        self.statLabel = CaptionLabel("")
        head.addWidget(self.statLabel)
        lay.addLayout(head)
        _notesDesc = CaptionLabel("你的顿悟与想法——第一人称思想层，所有 agent 检索可见；"
                                  "正文打 #标签 可分类串成演化线；全局热键 Ctrl+Alt+N 随时呼出")
        _notesDesc.setWordWrap(True)
        lay.addWidget(_notesDesc)

        # 心情色带热力图：一格一天，颜色=当日心情/有无记录（近半年，悬停看详情）
        lay.addSpacing(8)
        self.heatRow = QHBoxLayout()
        self.heatRow.setSpacing(0)
        self._heat_cells = []
        for _ in range(180):
            cell = QLabel()
            cell.setFixedSize(3, 18)  # 4→3px：180 格半年图总宽 540（原 720 在 920 窄窗下超限，2026-10-10 溢出体检）
            self.heatRow.addWidget(cell)
            self._heat_cells.append(cell)
        lay.addLayout(self.heatRow)

        # 轻捕获：零决策输入区（无标题无模板，写完即存）
        lay.addSpacing(16)
        self.noteInput = TextEdit()
        self.noteInput.setPlaceholderText("随手记一条想法/顿悟/摘录…（正文打 #标签 可分类）\n"
                                          "Ctrl+Enter 快速保存 · 输入停顿会浮出你可能想起的旧想法")
        self.noteInput.setFixedHeight(76)
        lay.addWidget(self.noteInput)
        row = QHBoxLayout()
        self.moodPicker = MoodPicker()
        row.addWidget(self.moodPicker)
        self.capsuleCombo = ComboBox()
        self.capsuleCombo.addItems(["立即开启", "7 天后开启", "30 天后开启", "一年后开启"])
        self.capsuleCombo.setFixedWidth(124)
        self.capsuleCombo.setToolTip("时光胶囊：设一个开启日，到期前这条随记对检索隐藏")
        row.addWidget(self.capsuleCombo)
        row.addStretch(1)
        self.noteBtn = PrimaryPushButton(ic("SEND", "INFO"), "记下")
        self.noteBtn.setEnabled(False)  # 空内容灰禁用（memos 模式），视觉引导+省无效点击
        self.noteBtn.clicked.connect(self.save_note)
        row.addWidget(self.noteBtn)
        lay.addLayout(row)
        self.noteInput.textChanged.connect(self._on_input_changed)

        # 实时关联浮出：写新想法时浮出你可能想起的旧随记与记忆
        self.relateBox = QVBoxLayout()
        self.relateBox.setContentsMargins(4, 8, 4, 0)
        self.relateBox.setSpacing(4)
        self.relatePanel = QWidget()
        self.relatePanel.setLayout(self.relateBox)
        self.relatePanel.setVisible(False)
        lay.addWidget(self.relatePanel)

        # 不期而遇：每日随机回顾（当日固定，收藏优先）
        lay.addSpacing(16)
        self.recallHead = StrongBodyLabel("不期而遇——今天随机想起的过去")
        self.recallHead.setVisible(False)
        lay.addWidget(self.recallHead)
        self.recallBox = QVBoxLayout()
        self.recallBox.setContentsMargins(4, 4, 4, 0)
        self.recallBox.setSpacing(6)
        lay.addLayout(self.recallBox)

        # 时光回放：往年今天
        self.otdHead = StrongBodyLabel("时光回放——往年今天")
        self.otdHead.setVisible(False)
        lay.addWidget(self.otdHead)
        self.otdBox = QVBoxLayout()
        self.otdBox.setContentsMargins(4, 4, 4, 0)
        self.otdBox.setSpacing(6)
        lay.addLayout(self.otdBox)

        # 胶囊到期
        self.capsuleHead = StrongBodyLabel("胶囊到期——封存的想法到货了")
        self.capsuleHead.setVisible(False)
        lay.addWidget(self.capsuleHead)
        self.capsuleBox = QVBoxLayout()
        self.capsuleBox.setContentsMargins(4, 4, 4, 0)
        self.capsuleBox.setSpacing(6)
        lay.addLayout(self.capsuleBox)

        # 时间线主体：按日期分组的无框行流（行是线不是卡）+ 整理模式（素材勾选）
        self.pick_mode = False
        self.picked = set()
        headRow = QHBoxLayout()
        self.tlHead = StrongBodyLabel("全部随记")
        headRow.addWidget(self.tlHead)
        headRow.addStretch(1)
        self.pickToggle = ClickBodyLabel("整理")
        self.pickToggle.setToolTip("勾选几条随记，生成整理指令交给 agent，提炼成一条记忆（NoteGen 式）")
        self.pickToggle.clicked.connect(self._toggle_pick)
        headRow.addWidget(self.pickToggle)
        lay.addSpacing(16)
        lay.addLayout(headRow)
        # 整理模式操作条（默认隐藏）
        self.pickBar = QWidget()
        pb = QHBoxLayout(self.pickBar)
        pb.setContentsMargins(4, 4, 4, 4)
        self.pickCount = CaptionLabel("已选 0 条")
        pb.addWidget(self.pickCount)
        pb.addStretch(1)
        self.organizeBtn = PushButton("生成整理指令（复制）")
        self.organizeBtn.setEnabled(False)
        self.organizeBtn.clicked.connect(self._gen_organize)
        pb.addWidget(self.organizeBtn)
        cancelPick = PushButton("取消")
        cancelPick.clicked.connect(self._toggle_pick)
        pb.addWidget(cancelPick)
        self.pickBar.setVisible(False)
        self.pickBar.setStyleSheet(f"background:{tokens.C(tokens.ACCENT_SOFT)};border-radius:4px;")
        lay.addWidget(self.pickBar)
        self.tlBox = QVBoxLayout()
        self.tlBox.setContentsMargins(4, 12, 16, 24)
        self.tlBox.setSpacing(16)
        lay.addLayout(self.tlBox)
        lay.addStretch(1)
        self.scroll.setWidget(body)
        self.scroll.enableTransparentBackground()  # 必须在 setWidget 后（qfw API 需 widget 已存在才命中 body；2026-10-10 深底错配根因）
        outer.addWidget(self.scroll, 1)
        QShortcut(QKeySequence("Ctrl+Return"), self.noteInput, self.save_note)
        self._restore_draft()

    # ---- 输入联动：灰禁用 + 实时关联防抖 + 草稿防抖 ----
    def _on_input_changed(self):
        self.noteBtn.setEnabled(bool(self.noteInput.toPlainText().strip()))
        self._relate_timer.start()
        self._draft_timer.start()

    def _draft_path(self):
        return Path(self.win.root) / "_hub" / "note_draft.txt" if self.win.root else None

    def _save_draft(self):
        p = self._draft_path()
        if not p:
            return
        try:
            text = self.noteInput.toPlainText()
            if text.strip():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
            elif p.exists():
                p.unlink()  # 清空输入=草稿已处理，删文件
        except Exception:
            pass

    def _restore_draft(self):
        p = self._draft_path()
        if p and p.exists():
            try:
                txt = p.read_text(encoding="utf-8").strip()
                if txt:
                    self.noteInput.setPlainText(txt)
                    InfoBar.info("发现未保存的草稿", "已为你恢复上次输入，确认后记得「记下」",
                                 duration=5000, parent=self.win)
            except Exception:
                pass

    def _show_related(self):
        """实时关联浮出（Smart Connections 式）：正文 >=4 字时检索旧随记+记忆浮出预览。
        count_hits=False——预览不记唤起，避免打字刷高 use_count 污染数据。"""
        kw = self.noteInput.toPlainText().strip()
        while self.relateBox.count():
            it = self.relateBox.takeAt(0)
            w = it.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        if len(kw) < 4 or not self.win.root:
            self.relatePanel.setVisible(False)
            return
        lines = []
        try:
            for n in brain.search_notes(self.win.root, kw, limit=3):
                lines.append(("随记", n))
            for m in brain.search_memories(self.win.root, kw, "", 3, count_hits=False):
                lines.append(("记忆", m))
        except Exception:
            lines = []
        if not lines:
            self.relatePanel.setVisible(False)
            return
        self.relateBox.addWidget(CaptionLabel("你可能想起了——"))
        for kind, item in lines[:5]:
            line = QHBoxLayout()
            line.setSpacing(8)
            lb = QLabel(kind)
            lb.setStyleSheet(f"color:#fff;background:{tokens.BADGE_FALLBACK};border-radius:8px;"
                             "padding:1px 8px;font-size:11px;")
            lb.setFixedHeight(20)
            line.addWidget(lb)
            body = CaptionLabel(f"#{item['id']} {str(item.get('content', ''))[:90]}")
            body.setWordWrap(True)
            line.addWidget(body, 1)
            wrap = QWidget()
            wrap.setLayout(line)
            self.relateBox.addWidget(wrap)
        self.relatePanel.setVisible(True)

    def save_note(self):
        content = self.noteInput.toPlainText().strip()
        if not content:
            return  # 按钮空态已灰禁用，此路径仅防御（如程序化调用）
        mood = self.moodPicker.mood()
        tags = " ".join(sorted({t for t in re.findall(r"#([^\s#]+)", content)}))  # 内联 #标签
        capsule_days = {"7 天后开启": 7, "30 天后开启": 30,
                        "一年后开启": 365}.get(self.capsuleCombo.currentText())
        open_at = ""
        if capsule_days:
            open_at = (datetime.datetime.now() + datetime.timedelta(days=capsule_days)).strftime("%Y-%m-%d")
        try:
            ctx = brain.active_context(self.win.root)  # 随记自动附上下文：当时的活跃项目
        except Exception:
            ctx = ""
        try:
            nid = brain.add_note(self.win.root, content, tags=tags, mood=mood,
                                 source="human", open_at=open_at, context=ctx)
        except Exception as e:  # noqa: BLE001
            InfoBar.error("随记保存失败", str(e), duration=5000, parent=self.win)
            return
        self.noteInput.clear()
        self.moodPicker.reset()
        self.capsuleCombo.setCurrentIndex(0)
        self._save_draft()  # 空内容=清草稿文件
        self.reload()
        tip = (f"#{nid} 已封存为时光胶囊，{open_at} 开启" if open_at
               else f"#{nid}——思想已进大脑，所有 agent 检索可见")
        InfoBar.success("已记入随记", tip, duration=3000, parent=self.win)

    def _star(self, nid, on):
        try:
            brain.star_note(self.win.root, nid, on)
        except Exception:
            return
        self.reload()

    def _dig(self, nid):
        """深挖协议入口：生成提示词进剪贴板，agent 读随记后向主人反问（Rosebud 模式）。"""
        QApplication.clipboard().setText(
            f"请深挖我的随记 #{nid}：用 hub_note_read 读全文，"
            f"然后向我反问一个最能展开这个想法的问题")
        InfoBar.success("深挖提示词已复制", "粘贴给任意 agent，它会读随记并向你反问",
                        duration=4000, parent=self.win)

    def _toggle_pick(self):
        """整理模式：时间线行首出现勾选框，勾选素材生成整理指令（NoteGen 第②③步）。"""
        self.pick_mode = not self.pick_mode
        if not self.pick_mode:
            self.picked = set()
        self.pickBar.setVisible(self.pick_mode)
        self.pickToggle.setText("退出整理" if self.pick_mode else "整理")
        self.reload()

    def _pick_toggle(self, nid, on):
        if on:
            self.picked.add(nid)
        else:
            self.picked.discard(nid)
        self.pickCount.setText(f"已选 {len(self.picked)} 条")
        self.organizeBtn.setEnabled(bool(self.picked))

    def _gen_organize(self):
        if not self.picked:
            return
        ids = " ".join(f"#{i}" for i in sorted(self.picked))
        QApplication.clipboard().setText(
            f"请整理我的随记：{ids}。用 hub_note_read 逐条读，提炼共同主题成一条记忆，"
            f"用 hub_memory_write 写入（带 tags），写完告诉我你提炼出了什么")
        InfoBar.success("整理指令已复制", "粘贴给任意 agent，提炼出的记忆会进大脑",
                        duration=4000, parent=self.win)

    def _filter_tag(self, tag):
        self._tag_filter = "" if self._tag_filter == tag else tag
        self.reload()

    def _note_row(self, n, dim=False, star=True):
        """通用行：收藏星 + 相对时间 + 心情徽章 + 标签胶囊 + 深挖 + 上下文 + 正文。
        时光胶囊未到期灰显+倒计时；标签胶囊点击=串成该主题的思想演化线。"""
        line = QHBoxLayout()
        line.setSpacing(8)
        if self.pick_mode:
            cb = QCheckBox()
            cb.setChecked(n["id"] in self.picked)
            cb.stateChanged.connect(lambda st, nid=n["id"]: self._pick_toggle(nid, bool(st)))
            line.addWidget(cb)
        if star:
            s = ClickBodyLabel("★" if n.get("starred") else "☆")
            s.setToolTip("收藏后进每日回顾优先池")
            s.clicked.connect(lambda _, nid=n["id"], on=not n.get("starred"): self._star(nid, on))
            line.addWidget(s)
        line.addWidget(CaptionLabel(rel_time(n["ts"])))
        if n.get("mood"):
            line.addWidget(mood_badge(n["mood"]))
        for t in (n.get("tags") or "").split():
            tg = ClickBodyLabel(f"#{t}")
            tg.setStyleSheet(f"color:{tokens.C(tokens.ACCENT)};background:{tokens.C(tokens.ACCENT_SOFT)};"
                             f"border-radius:8px;padding:1px 8px;font-size:11px;")
            tg.setFixedHeight(20)
            tg.clicked.connect(lambda _, tt=t: self._filter_tag(tt))
            line.addWidget(tg)
        if dim:
            try:
                left = (date.fromisoformat(n["open_at"]) - date.today()).days
                line.addWidget(CaptionLabel(f"{left} 天后开启"))
            except ValueError:
                pass
        dig = ClickBodyLabel("深挖")
        dig.setStyleSheet(f"color:{tokens.C(tokens.ACCENT)};font-size:11px;")
        dig.setToolTip("生成提示词：agent 会读这条随记并向你反问，把想法展开")
        dig.clicked.connect(lambda _, nid=n["id"]: self._dig(nid))
        line.addWidget(dig)
        if n.get("context"):
            cl = CaptionLabel(f"· {n['context']}")
            cl.setToolTip("保存时自动附上的上下文：当时的活跃项目")
            line.addWidget(cl)
        body_l = BodyLabel(n["content"])
        body_l.setWordWrap(True)
        if dim:
            body_l.setStyleSheet(f"color:{tokens.C(tokens.TEXT_3)};")  # 未到期胶囊灰显（本体可见、检索隐藏）
        line.addWidget(body_l, 1)
        wrap = QWidget()
        wrap.setLayout(line)
        return wrap

    def _reload_heatmap(self, root):
        try:
            data = brain.notes_heatmap(root, days=len(self._heat_cells))
        except Exception:
            return
        dim_gray = tokens.C(tokens.STROKE)
        for cell, (d, cnt, mood) in zip(self._heat_cells, data):
            if cnt and mood:
                color = MOOD_COLOR.get(mood, tokens.C(tokens.SUCCESS))
            elif cnt:
                color = tokens.C(tokens.SUCCESS)
            else:
                color = dim_gray
            cell.setStyleSheet(f"background:{color};border-radius:1px;")
            cell.setToolTip(f"{d}  {cnt} 条" + (f"  {mood}" if mood else ""))

    def showEvent(self, e):  # 切入即重读：agent 侧 hub_note 写入后 GUI 跟上
        super().showEvent(e)
        self.reload()

    def reload(self):
        root = self.win.root
        if not root:
            return
        try:
            notes = brain.list_notes(root, limit=200)
        except Exception:
            return
        try:
            self.statLabel.setText(f"累计 {brain.notes_count(root)} 条 · "
                                   f"本月 {brain.notes_count(root, month=True)} 条")
        except Exception:
            self.statLabel.setText("")
        self._reload_heatmap(root)
        # 主题串过滤：同一标签的随记按时间串成思想演化线
        tf = self._tag_filter
        shown = [n for n in notes if not tf or tf in (n.get("tags") or "").split()]
        self.tlHead.setText(f"主题串 #{tf}——这条线的思想演化（{len(shown)} 条，再点标签退出）"
                            if tf else "全部随记")
        # 不期而遇
        clear_layout(self.recallBox)
        try:
            rec = brain.daily_recall_notes(root, k=4)
        except Exception:
            rec = []
        self.recallHead.setVisible(bool(rec) and not tf)
        for n in rec:
            self.recallBox.addWidget(self._note_row(n))
        # 时光回放
        clear_layout(self.otdBox)
        try:
            otd = brain.on_this_day_notes(root)
        except Exception:
            otd = []
        self.otdHead.setVisible(bool(otd))
        for n in otd[:5]:
            self.otdBox.addWidget(self._note_row(n))
        # 胶囊到期
        clear_layout(self.capsuleBox)
        try:
            due = brain.capsule_notes(root, due_only=True)
        except Exception:
            due = []
        self.capsuleHead.setVisible(bool(due))
        for n in due[:5]:
            self.capsuleBox.addWidget(self._note_row(n))
        # 时间线
        self.scroll.verticalScrollBar().setValue(0)
        clear_layout(self.tlBox)
        if not shown:
            wrap = QWidget()
            v = QVBoxLayout(wrap)
            v.setContentsMargins(0, 12, 0, 0)
            v.addWidget(empty_state(
                "EDIT", "还没有随记",
                "在上面记第一条，或直接对任何 agent 说「记一下：…」"))
            self.tlBox.addWidget(wrap)
            return
        today = date.today().isoformat()
        cur, box = None, None
        for n in shown:
            d = (n["ts"] or "")[:10] or "日期未标注"
            if d != cur:
                cur = d
                group = QWidget()
                g = QVBoxLayout(group)
                g.setContentsMargins(0, 0, 0, 0)
                g.setSpacing(8)
                g.addWidget(StrongBodyLabel(d))
                box = g
                self.tlBox.addWidget(group)
            dim = bool(n.get("open_at")) and n["open_at"] > today
            box.addWidget(self._note_row(n, dim=dim))

