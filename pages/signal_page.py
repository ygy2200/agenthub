# -*- coding: utf-8 -*-
"""SignalPage（GUI 管理面补全·v2.37）：信号中心——M3 的桌面端。

hub_signal_send/signals/signal_read 的 GUI 呈现：发送区（选身份、选收件人、写内容）
+ 收件箱（未读置顶、可标已读）+ 已发送。桌面端也能参与 agent 间通信——
不必开 MCP 会话。

R167：列表只够放摘要，正文 56 字截断后没有地方看全文（真实 #1 正文 1169 字）；
改左列表右全文预览，时间走 rel_time，已读按钮只在选中未读时可用，空库给引导。
"""
from __future__ import annotations

import html as _html

from PySide6.QtWidgets import QListWidgetItem, QSpacerItem

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401
import brain


class SignalPage(QWidget):
    """信号中心：发送 + 收件箱 + 已发送（SegmentedWidget 切换）。"""

    EMPTY_TITLE = "暂无信号"
    EMPTY_DESC = "顶部选好身份与收件人，写一条消息回车发送；对方下次开工会在心跳里收到未读提醒。"
    EMPTY_ACTION = "写第一条消息"

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self._rows: list = []       # 当前 tab 的信号行（与列表行一一对应）
        self._error = ""
        self._last_sel = {"inbox": "", "sent": ""}  # 每个 tab 各记最后选中，切回来不丢位置
        self._cur_me = None
        lay = QVBoxLayout(self)
        set_page_margins(lay)
        lay.addWidget(_page_head(
            "信号中心", "agent 之间的消息：发给指定 agent 或广播——对方下次开工即在心跳里"
                        "收到未读提醒；已读有回执。桌面端可直接发、可查收、可标已读"))

        # —— 发送区 ——
        # 2026-10-15 第三轮：信号中心原整页零图标，控件平铺一段到底。
        # 注意：本页是无滚动的紧凑表单，加整行 section_head 会在 920x600 下把
        # 表单区压扁（布局压测实测）——改为给既有标签配图标，不增行高。
        sendBox = QWidget()
        sl = QHBoxLayout(sendBox)
        sl.setContentsMargins(0, 0, 0, 0)
        _i1 = IconWidget(ic("PEOPLE", "INFO"), sendBox); _i1.setFixedSize(16, 16)
        sl.addWidget(_i1)
        sl.addWidget(StrongBodyLabel("我是"))
        self.meCombo = ComboBox()
        self.meCombo.setMinimumWidth(110)
        sl.addWidget(self.meCombo)
        _i2 = IconWidget(ic("SEND", "INFO"), sendBox); _i2.setFixedSize(16, 16)
        sl.addWidget(_i2)
        sl.addWidget(StrongBodyLabel("发给"))
        self.toCombo = ComboBox()
        self.toCombo.setMinimumWidth(110)
        sl.addWidget(self.toCombo)
        # R127：下拉显示纯中文（原来「msg 消息」把内部枚举值露给用户）；
        # 发送按索引映射回枚举，数据格式不变。
        self._kinds = [("msg", "消息"), ("alert", "告警"), ("ack_request", "需回执")]
        self.kindCombo = ComboBox()
        self.kindCombo.addItems([label for _, label in self._kinds])
        sl.addWidget(self.kindCombo)
        self.edit = LineEdit()
        self.edit.setPlaceholderText("消息内容（回车发送）")
        sl.addWidget(self.edit, 1)
        self.sendBtn = PushButton(ic("SEND", "INFO"), "发送")
        self.sendBtn.clicked.connect(self.send)
        sl.addWidget(self.sendBtn)
        lay.addWidget(sendBox)

        # —— 收件箱 / 已发送 ——
        segRow = QHBoxLayout()
        self.seg = SegmentedWidget(self)
        self.seg.addItem("inbox", "收件箱")
        self.seg.addItem("sent", "已发送")
        self.seg.setCurrentItem("inbox")  # 两 tab 都不高亮时用户不知道当前在哪一页
        # 空态时 split 隐藏，剩余高度会摊给竖向 Minimum 的 tab 栏（实测撑到 110px，
        # 背景胶囊/文字/指示条分成三层）；锁到 qfw 自然高度（随字体 26~31px）。
        self.seg.setFixedHeight(self.seg.sizeHint().height())
        segRow.addWidget(self.seg)
        segRow.addStretch(1)
        self.sumLabel = CaptionLabel("", self)
        # guard_overflow 会把 >16 字的 Caption 设成 Ignored（sizeHint=0），有 stretch 的行里
        # 会按 0 宽分配再被最小宽度撑到右边界外（R167 实测计数被裁）。这里保住宽度。
        self.sumLabel.setProperty("keepWidth", True)
        self.sumLabel.setMaximumWidth(280)
        self.sumLabel.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        segRow.addWidget(self.sumLabel)
        lay.addLayout(segRow)

        # 左列表右全文：原列表单行只放得下 56 字，长消息没有阅读入口
        split = QSplitter(Qt.Horizontal)
        self.listw = ListWidget()
        self.listw.setMinimumHeight(240)
        split.addWidget(self.listw)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(tokens.SPACE_S)
        act = QHBoxLayout()
        self.readBtn = PushButton("标记已读")
        self.readBtn.clicked.connect(self.mark_read)
        act.addWidget(self.readBtn)
        self.copyBtn = PushButton(ic("COPY", "INFO"), "复制内容")
        self.copyBtn.clicked.connect(self.copy_current)
        act.addWidget(self.copyBtn)
        act.addStretch(1)
        rv.addLayout(act)
        self.preview = TextBrowser()
        self.preview.setHtml(self._intro_html())
        rv.addWidget(self.preview, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
        split.setSizes([520, 420])
        lay.addWidget(split, 1)
        self.lay = lay
        self.split = split
        self.split.setVisible(False)

        row = QHBoxLayout()
        # R113：原文案 41 字被 920 窄窗裁成「刷新随全窗扫描（」——截断比省略更糟
        tip = CaptionLabel("F5 刷新 · @提醒随心跳送达")
        tip.setToolTip("刷新随全窗扫描（F5）；@提醒靠对方心跳的「[未读信号]」段送达")
        row.addWidget(tip)
        row.addStretch(1)
        refreshBtn = PushButton(ic("SYNC", "INFO"), "刷新")
        refreshBtn.clicked.connect(self.reload)
        row.addWidget(refreshBtn)
        lay.addLayout(row)

        # 空态没有 split 占伸缩位：显式弹簧兜住多余高度，空态保持自然高度。
        self._empty_gap = QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
        self._gap_added = False

        self.listw.currentRowChanged.connect(lambda _r: self.show_detail())
        self.seg.currentItemChanged.connect(lambda _k: self.reload())
        self.meCombo.currentIndexChanged.connect(lambda _i: self.reload())
        self.edit.returnPressed.connect(self.send)
        self._sync_actions(None)
        list_empty_guard(self.listw, lay, self.EMPTY_TITLE, self.EMPTY_DESC,
                         index=lay.indexOf(split),
                         action_text=self.EMPTY_ACTION, action=self._focus_editor)

    # ---------- 数据 ----------
    def _focus_editor(self):
        self.edit.setFocus()

    def _intro_html(self):
        return (f"<p style='color:{tokens.C(tokens.TEXT_3)}'>选中左侧一条信号，"
                "这里显示完整正文与已读回执——长消息不会被截断。<br>"
                "未读信号可用「标记已读」记回执；「复制内容」取全文。</p>")

    def _tab_key(self):
        return "sent" if self.seg.currentRouteKey() == "sent" else "inbox"

    def reload(self):
        prev_sel = self._sel_id()
        self.listw.blockSignals(True)
        self.listw.clear()
        self._rows = []
        self._error = ""
        if not self.win.root:
            self.listw.blockSignals(False)
            self._finish_reload(prev_sel)
            self.sumLabel.setText("未选择大脑目录")
            self.preview.setHtml(md_to_html(
                "**未选择大脑目录**\n\n先到「接入」页选择数据目录，再回来收发信号。"))
            return
        # 身份下拉：在册 agents（heartbeat/log_work 自动注册）+ 常见名兜底
        try:
            names = [a["name"] for a in brain.list_agents(self.win.root)]
        except Exception:  # noqa: BLE001
            names = []
        for extra in ("workbuddy", "zcode", "dsh", "claude", "codex", "hermes"):
            if extra not in names:
                names.append(extra)
        keep_me = self.meCombo.currentText()
        keep_to = self.toCombo.currentText()
        self.meCombo.blockSignals(True)
        self.toCombo.blockSignals(True)
        self.meCombo.clear()
        self.meCombo.addItems(names)
        self.toCombo.clear()
        self.toCombo.addItem("（广播：全员可见）")
        self.toCombo.addItems(names)
        if keep_me in names:
            self.meCombo.setCurrentText(keep_me)
        if keep_to in names or keep_to == "（广播：全员可见）":
            self.toCombo.setCurrentText(keep_to)
        self.meCombo.blockSignals(False)
        self.toCombo.blockSignals(False)

        me = self.meCombo.currentText()
        if me != self._cur_me:  # 换身份=换收件箱，不沿用上一位的选中
            self._last_sel = {"inbox": "", "sent": ""}
            self._cur_me = me
            prev_sel = ""
        keep_id = self._last_sel.get(self._tab_key()) or prev_sel
        sent = self.seg.currentRouteKey() == "sent"
        try:
            rows = (brain.signal_sent(self.win.root, me) if sent
                    else brain.signal_inbox(self.win.root, me))
        except Exception as e:  # noqa: BLE001
            self._error = f"读取失败：{e}"
            self.listw.addItem(self._error)
            self.listw.blockSignals(False)
            self.sumLabel.setText("读取失败")
            self.split.setVisible(True)
            self.preview.setHtml(md_to_html(f"**{self._error}**\n\n可点右下「刷新」重试；"
                                            "若持续失败，检查数据目录是否可读。"))
            self._sync_actions(None)
            list_empty_guard(self.listw, self.lay, self.EMPTY_TITLE, self.EMPTY_DESC,
                             index=self.lay.indexOf(self.split),
                             action_text=self.EMPTY_ACTION, action=self._focus_editor)
            return
        self._rows = rows
        for s in rows:
            to = f"→{s['to_agent']}" if s["to_agent"] else "→广播"
            one = " ".join((s.get("content") or "").split())
            if sent:
                txt = (f"#{s['id']} {rel_time(s['ts'])} {to}"
                       f"（{self._kind_label(s['kind'])}）{one[:60]}")
            else:
                dot = "●" if not s["_read"] else "·"
                proj = f" [{s['project']}]" if s["project"] else ""
                stale = " ！陈旧" if s.get("_stale") else ""
                txt = (f"{dot} #{s['id']} {rel_time(s['ts'])} {s['from_agent']}{to}{proj}"
                       f"（{self._kind_label(s['kind'])}）{one[:56]}{stale}")
            it = QListWidgetItem(txt)
            it.setToolTip(s["content"])
            self.listw.addItem(it)
        self.listw.blockSignals(False)
        self._finish_reload(keep_id)

    def _finish_reload(self, keep_id: str):
        sent = self.seg.currentRouteKey() == "sent"
        n = len(self._rows)
        self._set_empty_gap(n == 0)
        if sent:
            self.sumLabel.setText(f"已发送 {n} 条")
        else:
            unread = sum(1 for r in self._rows if not r["_read"])
            stale = sum(1 for r in self._rows if r.get("_stale"))
            self.sumLabel.setText(f"共 {n} 条 · 未读 {unread}"
                                  + (f"（陈旧 {stale}）" if stale else ""))
        if n:
            self.split.setVisible(True)
            idx = next((i for i, r in enumerate(self._rows) if str(r["id"]) == keep_id), -1)
            if idx < 0:
                if sent:
                    idx = 0
                else:
                    # 优先新鲜未读：陈旧未读先不抢焦点，但仍标「！陈旧」留在列表里
                    idx = next((i for i, r in enumerate(self._rows)
                                if not r["_read"] and not r.get("_stale")), -1)
                    if idx < 0:
                        idx = next((i for i, r in enumerate(self._rows)
                                    if not r["_read"]), 0)
            self.listw.setCurrentRow(idx)
        else:
            self.split.setVisible(False)
            self.preview.setHtml(self._intro_html())
            self._sync_actions(None)
        list_empty_guard(self.listw, self.lay, self.EMPTY_TITLE, self.EMPTY_DESC,
                         index=self.lay.indexOf(self.split),
                         action_text=self.EMPTY_ACTION, action=self._focus_editor)

    def _set_empty_gap(self, on: bool):
        if on == self._gap_added:
            return
        if on:
            self.lay.insertItem(self.lay.indexOf(self.split) + 1, self._empty_gap)
        else:
            self.lay.removeItem(self._empty_gap)
        self._gap_added = on

    def _selected(self):
        row = self.listw.currentRow()
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def _sel_id(self) -> str:
        s = self._selected()
        return str(s["id"]) if s else ""

    def show_detail(self):
        s = self._selected()
        if not s:
            self.preview.setHtml(md_to_html(f"**{self._error}**") if self._error
                                 else self._intro_html())
            self._sync_actions(None)
            return
        self._last_sel[self._tab_key()] = str(s["id"])
        self.preview.setHtml(self._detail_html(s))
        self._sync_actions(s)

    def _detail_html(self, s) -> str:
        sent = self.seg.currentRouteKey() == "sent"
        to = f"→{s['to_agent']}" if s["to_agent"] else "→广播"
        meta = f"{rel_time(s['ts'])} · {s['from_agent']}{to}"
        if s.get("project"):
            meta += f" · [{s['project']}]"
        if sent:
            if s.get("status") == "read" or s.get("read_at"):
                who = s.get("read_by") or "?"
                state = f"√ 已读回执 · {who} {rel_time(s.get('read_at') or '')}"
            else:
                state = "· 未读回执（对方下次开工会在心跳里看到提醒）"
        elif s.get("_read"):
            state = "· 已读"
        else:
            state = "● 未读"
            if s.get("_stale"):
                state += f" · ！陈旧（超过 {brain.SIGNAL_STALE_DAYS} 天未读）"
        return (f"<h3>#{s['id']} {self._kind_label(s['kind'])}</h3>"
                f"<p style='color:{tokens.C(tokens.TEXT_3)}'>{_html.escape(meta)}</p>"
                f"<p style='color:{tokens.C(tokens.TEXT_2)}'>{_html.escape(state)}</p>"
                f"<hr>" + md_to_html(s.get("content") or ""))

    # ---------- 操作 ----------
    @staticmethod
    def _kind_label(kind: str) -> str:
        kinds = dict([("msg", "消息"), ("alert", "告警"), ("ack_request", "需回执")])
        return kinds.get((kind or "").strip(), kind or "消息")

    def _sync_actions(self, s):
        sent = self.seg.currentRouteKey() == "sent"
        can_read = bool(s) and not sent and not s.get("_read")
        self.readBtn.setEnabled(can_read)
        self.readBtn.setToolTip("给选中的未读信号记回执（幂等）" if can_read
                                else "选中「收件箱」里一条未读信号后可标记")
        self.copyBtn.setEnabled(bool(s))

    def send(self):
        if not self.win.root:
            return
        content = self.edit.text().strip()
        if not content:
            InfoBar.warning("内容为空", "写点什么再发送", parent=self)
            return
        me = self.meCombo.currentText()
        to_text = self.toCombo.currentText()
        to = "" if to_text.startswith("（广播") else to_text
        kind = self._kinds[self.kindCombo.currentIndex()][0]
        try:
            tip, sid = brain.signal_send(self.win.root, me, content, to_agent=to,
                                         kind=kind)
        except Exception as e:  # noqa: BLE001
            InfoBar.error("发送失败", str(e), parent=self)
            return
        if not sid:
            InfoBar.error("发送失败", tip, parent=self)
            return
        InfoBar.success("已发送", f"#{sid} {'→广播' if not to else '→' + to}", parent=self)
        self.edit.clear()
        self.reload()

    def mark_read(self):
        s = self._selected()
        if not s or self.seg.currentRouteKey() == "sent":
            InfoBar.warning("先选中一条信号", "在「收件箱」里选中一条未读信号，再标已读", parent=self)
            return
        me = self.meCombo.currentText()
        try:
            msg = brain.signal_mark_read(self.win.root, s["id"], me)
        except Exception as e:  # noqa: BLE001
            op_failed("标记已读", e, parent=self)
            return
        InfoBar.success("已读回执", msg, parent=self)
        self.reload()

    def copy_current(self):
        s = self._selected()
        if not s:
            return
        QApplication.clipboard().setText(s.get("content") or "")
        InfoBar.success("已复制信号内容", f"#{s['id']}", duration=2000, parent=self)
