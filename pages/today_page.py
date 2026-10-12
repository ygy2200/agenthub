# -*- coding: utf-8 -*-
"""TodayPage（M10·v2.30）：工作台——「今天该干什么」桌面视图。

hub_next 前沿队列的 GUI 呈现（同源 brain.task_next）：
  可认领（依赖已满足，按优先级 0急→3低 排序）/ 进行中 / 可接管（超 TTL 没人动）/ 等待依赖 四段。
只读看板 + 管理面操作（标记完成=空 agent 走 task_update 管理通道，done 触发 M2 自动结晶）。
认领/新建/改依赖走 MCP（hub_claim / hub_task_add）——桌面端不代理 agent 身份。
"""
from __future__ import annotations

from PySide6.QtWidgets import QListWidgetItem  # noqa: F401  （uikit 的 * 不含它，显式带）

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （运行期重绑名动态取）
import brain


_GROUP_ORDER = ("ready", "doing", "takeover", "waiting")  # 段序；中文名用 brain.TASK_GROUP_CN（同源）


class TodayPage(QWidget):
    """工作台：前沿队列四段一屏看全 + 管理通道标记完成。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self._item2task: dict = {}  # id(QListWidgetItem) -> task row（分组标题不进映射）
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.addWidget(_page_head(
            "工作台", "今天该干什么——前沿队列（hub_next 同源）：可认领按优先级排队，"
                      "进行中 / 可接管 / 等待依赖一屏看全；标记完成走管理通道（自动结晶）"))

        top = QHBoxLayout()
        self.sumLabel = CaptionLabel("")
        top.addWidget(self.sumLabel)
        top.addStretch(1)
        refreshBtn = PushButton(ic("SYNC", "INFO"), "刷新")
        refreshBtn.clicked.connect(self.reload)
        top.addWidget(refreshBtn)
        lay.addLayout(top)

        self.listw = ListWidget()
        self.listw.setMinimumHeight(320)
        lay.addWidget(self.listw, 1)

        row = QHBoxLayout()
        self.doneBtn = PushButton("标记完成")
        self.doneBtn.setToolTip("把选中的「进行中」任务标记完成（管理通道，空 agent）——完成自动结晶")
        self.doneBtn.clicked.connect(self.mark_done)
        row.addWidget(self.doneBtn)
        row.addWidget(CaptionLabel("认领 / 新建 / 改优先级：hub_next · hub_claim · hub_task_add（MCP 侧）"))
        row.addStretch(1)
        lay.addLayout(row)

    # ---------- 数据 ----------
    def reload(self):
        self.listw.clear()
        self._item2task.clear()
        if not self.win.root:
            self.sumLabel.setText("未配置数据根目录")
            return
        try:
            nxt = brain.task_next(self.win.root, limit=20)
        except Exception as e:  # noqa: BLE001  看板读失败不该拖垮窗口刷新
            self.sumLabel.setText(f"读取失败：{e}")
            return
        c = nxt["counts"]
        self.sumLabel.setText(" · ".join(f"{brain.TASK_GROUP_CN[k]} {c[k]}" for k in _GROUP_ORDER))
        for key in _GROUP_ORDER:
            items = nxt[key]
            if not items:
                continue
            head = QListWidgetItem(f"── {brain.TASK_GROUP_CN[key]}（{len(items)}）")
            head.setFlags(Qt.ItemFlag.NoItemFlags)  # 分组标题不可选
            self.listw.addItem(head)
            for t in items:
                proj = f"{t['project']}｜" if t["project"] else ""
                who = f"  @{t['owner']}" if t["owner"] else ""
                due = f"  截止:{t['due']}" if t.get("due") else ""
                extra = ""
                if key == "waiting" and t.get("_wait"):
                    extra = "  等待:" + ",".join(f"#{w}" for w in t["_wait"])
                it = QListWidgetItem(
                    f"  #{t['id']} [P{t['_priority']}] {proj}{t['title'][:60]}{who}{due}{extra}")
                self._item2task[id(it)] = t
                self.listw.addItem(it)

    def _selected_task(self):
        it = self.listw.currentItem()
        return self._item2task.get(id(it)) if it else None

    # ---------- 操作 ----------
    def mark_done(self):
        t = self._selected_task()
        if not t:
            InfoBar.warning("先选中任务", "选中「进行中」段里的任务再标记完成", parent=self)
            return
        if t["status"] != "in_progress":
            InfoBar.warning("状态不符",
                            f"#{t['id']} 当前是 {t['status']}，只有进行中可标记完成", parent=self)
            return
        try:
            err, row = brain.task_update(self.win.root, t["id"], status="done", agent="")
        except Exception as e:  # noqa: BLE001
            InfoBar.error("标记失败", str(e), parent=self)
            return
        if err:
            InfoBar.error("标记失败", err, parent=self)
            return
        tip = "已标记完成" + ("（已自动结晶）" if row.get("crystal") else "")
        InfoBar.success("任务完成", f"#{t['id']} {tip}", parent=self)
        self.reload()
