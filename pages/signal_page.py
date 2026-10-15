# -*- coding: utf-8 -*-
"""SignalPage（GUI 管理面补全·v2.37）：信号中心——M3 的桌面端。

hub_signal_send/signals/signal_read 的 GUI 呈现：发送区（选身份、选收件人、写内容）
+ 收件箱（未读置顶、可标已读）+ 已发送。桌面端也能参与 agent 间通信——
不必开 MCP 会话。
"""
from __future__ import annotations

from PySide6.QtWidgets import QListWidgetItem, QInputDialog  # noqa: F401

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401
import brain


class SignalPage(QWidget):
    """信号中心：发送 + 收件箱 + 已发送（SegmentedWidget 切换）。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self._inbox_items: dict = {}   # id(item) -> signal row（收件箱）
        self._sent_items: dict = {}
        lay = QVBoxLayout(self)
        set_page_margins(lay)
        lay.addWidget(_page_head(
            "信号中心", "agent 间的消息（M3）：发给指定 agent 或广播——对方下次开工即在心跳里"
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
        self.kindCombo = ComboBox()
        self.kindCombo.addItems(["msg 消息", "alert 告警", "ack_request 需回执"])
        sl.addWidget(self.kindCombo)
        self.edit = LineEdit()
        self.edit.setPlaceholderText("消息内容（回车发送）")
        sl.addWidget(self.edit, 1)
        self.sendBtn = PushButton(ic("SEND", "INFO"), "发送")
        self.sendBtn.clicked.connect(self.send)
        sl.addWidget(self.sendBtn)
        lay.addWidget(sendBox)

        # —— 收件箱 / 已发送 ——
        # 分段控件自带图标位，不额外加行；滚动保护由 listw 承担
        self.seg = SegmentedWidget(self)
        self.seg.addItem("inbox", "收件箱")
        self.seg.addItem("sent", "已发送")
        self.listw = ListWidget()
        self.listw.setMinimumHeight(300)
        lay.addWidget(self.seg)
        lay.addWidget(self.listw, 1)

        row = QHBoxLayout()
        self.readBtn = PushButton("标记已读")
        self.readBtn.setToolTip("给选中的未读信号记回执（幂等）")
        self.readBtn.clicked.connect(self.mark_read)
        row.addWidget(self.readBtn)
        row.addWidget(CaptionLabel("刷新随全窗扫描（F5）；@提醒靠对方心跳的「[未读信号]」段"))
        row.addStretch(1)
        refreshBtn = PushButton(ic("SYNC", "INFO"), "刷新")
        refreshBtn.clicked.connect(self.reload)
        row.addWidget(refreshBtn)
        lay.addLayout(row)

        self.seg.currentItemChanged.connect(lambda _k: self.reload())
        self.edit.returnPressed.connect(self.send)

    # ---------- 数据 ----------
    def reload(self):
        self.listw.clear()
        self._inbox_items.clear()
        self._sent_items.clear()
        if not self.win.root:
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
        self.meCombo.clear()
        self.meCombo.addItems(names)
        self.toCombo.clear()
        self.toCombo.addItem("（广播：全员可见）")
        self.toCombo.addItems(names)
        if keep_me in names:
            self.meCombo.setCurrentText(keep_me)
        if keep_to in names or keep_to == "（广播：全员可见）":
            self.toCombo.setCurrentText(keep_to)

        me = self.meCombo.currentText()
        if self.seg.currentItem() == "sent":
            try:
                rows = brain.signal_sent(self.win.root, me)
            except Exception as e:  # noqa: BLE001
                self.listw.addItem(f"读取失败：{e}")
                return
            if not rows:
                self.listw.addItem("（还没有发出过信号）")
                return
            for s in rows:
                to = f"→{s['to_agent']}" if s["to_agent"] else "→广播"
                it = QListWidgetItem(
                    f"#{s['id']} {s['ts'][5:16]} {to}（{s['kind']}）{s['content'][:60]}")
                self._sent_items[id(it)] = s
                self.listw.addItem(it)
            return
        try:
            rows = brain.signal_inbox(self.win.root, me)
        except Exception as e:  # noqa: BLE001
            self.listw.addItem(f"读取失败：{e}")
            return
        if not rows:
            self.listw.addItem("（收件箱空）")
            return
        n_un = sum(1 for r in rows if not r["_read"])
        head = QListWidgetItem(f"── 共 {len(rows)} 条，未读 {n_un}（●未读 ·已读）")
        head.setFlags(Qt.ItemFlag.NoItemFlags)
        self.listw.addItem(head)
        for s in rows:
            dot = "●" if not s["_read"] else "·"
            to = f"→{s['to_agent']}" if s["to_agent"] else "→广播"
            proj = f" [{s['project']}]" if s["project"] else ""
            it = QListWidgetItem(
                f"{dot} #{s['id']} {s['ts'][5:16]} {s['from_agent']}{to}{proj}"
                f"（{s['kind']}）{s['content'][:56]}")
            self._inbox_items[id(it)] = s
            self.listw.addItem(it)

    def _selected(self):
        it = self.listw.currentItem()
        if self.seg.currentItem() == "sent":
            return self._sent_items.get(id(it)) if it else None
        return self._inbox_items.get(id(it)) if it else None

    # ---------- 操作 ----------
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
        kind = self.kindCombo.currentText().split(" ")[0]
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
        if not s or self.seg.currentItem() == "sent":
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
