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


# 段序；中文名用 brain.TASK_GROUP_CN（同源）。
# R176：原只渲染前四段，退避/延后中的任务在 GUI 里凭空消失；六段都要可见。
_GROUP_ORDER = ("ready", "doing", "takeover", "waiting", "backoff", "deferred")


class TodayPage(QWidget):
    """工作台：前沿队列四段一屏看全 + 管理通道标记完成。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self._item2task: dict = {}  # id(QListWidgetItem) -> task row（分组标题不进映射）
        lay = QVBoxLayout(self)
        set_page_margins(lay)
        lay.addWidget(_page_head(
            "工作台", "今天该干什么——可认领按优先级排队，进行中 / 可接管 / 等待依赖 / 退避 / 延后分段列清；"
                      "标记完成走管理通道（自动归档为成果记录）"))

        top = QHBoxLayout()
        self.sumLabel = CaptionLabel("")
        self.sumLabel.setWordWrap(True)
        # 占满整行：空标签构造时的 sizeHint 很小，若只按 Preferred 加进 HBox，
        # 六段摘要会被截成前两段（R176 真实窗口实测）。
        top.addWidget(self.sumLabel, 1)
        refreshBtn = PushButton(ic("SYNC", "INFO"), "刷新")
        refreshBtn.clicked.connect(self.reload)
        top.addWidget(refreshBtn)
        lay.addLayout(top)

        self.digestLine = CaptionLabel("")
        self.digestLine.setWordWrap(True)
        lay.addWidget(self.digestLine)

        self.listw = ListWidget()
        self.listw.setMinimumHeight(320)
        self.listw.setTextElideMode(Qt.ElideRight)
        self.listw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lay.addWidget(section_head("待办清单", "DATE_TIME"))
        lay.addWidget(self.listw, 1)

        row = QHBoxLayout()
        # R785：桌面端也能建任务——空态原来只教「让 agent 说一句」，没开 agent 时这一页无路可走。
        self.newBtn = PrimaryPushButton(ic("ADD", "INFO"), "新建任务…")
        self.newBtn.setToolTip("以桌面管理面（gui）署名建任务：创建后进「可认领」，agent 下次开工报到可见")
        self.newBtn.clicked.connect(self.new_task)
        row.addWidget(self.newBtn)
        self.doneBtn = PushButton("标记完成")
        self.doneBtn.setToolTip("把选中的「进行中」任务标记完成（管理通道，空 agent）——完成自动结晶")
        self.doneBtn.clicked.connect(self.mark_done)
        self.listw.currentItemChanged.connect(lambda *_: self._sync_actions())
        row.addWidget(self.doneBtn)
        # R197：界面上只留人话，精确工具名进 tooltip（R171 同款口径）——「hub_next ·
        # hub_claim · hub_task_add」对用户是天书，对 agent 才是命令。
        cap = CaptionLabel("认领、新建、改优先级请让 agent 做（桌面端不代理 agent 身份）")
        cap.setToolTip("对应工具：hub_next（看下一步）· hub_claim（认领）· hub_task_add（新建）")
        row.addWidget(cap)
        row.addStretch(1)
        lay.addLayout(row)

    # ---------- 数据 ----------
    def reload(self):
        """统一入口：先填再同步空态——保证「未配置根目录 / 读取失败」等提前 return 也有引导。"""
        self._fill()
        list_empty_guard(self.listw, self.layout(),
                         "前沿队列为空",
                         "还没有任务——点「新建任务…」建一条，或让 agent 说一句「新建任务 …」；"
                         "它也会从工作记录的「待办」句子里自动提取",
                         action_text="新建任务…", action=self.new_task)
        self._sync_actions()

    def new_task(self):
        if not self.win.root:
            InfoBar.warning("未设置根目录", "请先到设置页选择", duration=2500, parent=self.win)
            return
        try:
            projects = [p["name"] for p in brain.list_projects(self.win.root, 200)]
        except Exception:  # noqa: BLE001 项目下拉拿不到不该挡住建任务
            projects = []
        dlg = NewTaskDialog(self.win, projects)
        if not dlg.exec():
            return
        v = dlg.values()
        err, tid = self.create_task(v["title"], v["project"], v["priority"], v["due"])
        if err:
            InfoBar.error("创建失败", err, duration=4000, parent=self.win)
            return
        InfoBar.success("已创建任务", f"#{tid} {v['title']}（可认领）", duration=2500,
                        parent=self.win)
        self.reload()

    def create_task(self, title, project="", priority=2, due=""):
        """建任务（对话框与用例共用）：署名 gui 走管理通道，返回 (错误, id)。"""
        return brain.task_add(self.win.root, title=title, project=project,
                              priority=priority, due=due, agent="gui")

    def _sync_actions(self):
        """R198：只有选中「进行中」的任务才让「标记完成」可点。

        原来按钮恒亮，空队列下点它只弹一句「先选中一个任务」——按钮亮着却什么也做不了，
        用户会以为标记完成坏了。与 R166/R192/R193 同一口径：动作跟随选中态。
        """
        t = self._selected_task()
        self.doneBtn.setEnabled(bool(t) and t.get("status") == "in_progress")

    def _fill(self):
        self.listw.clear()
        self._item2task.clear()
        if not self.win.root:
            self.sumLabel.setText("未配置数据根目录")
            self.digestLine.setText("")
            return
        try:
            nxt = brain.task_next(self.win.root, limit=20)
        except Exception as e:  # noqa: BLE001  看板读失败不该拖垮窗口刷新
            self.sumLabel.setText(f"读取失败：{e}")
            return
        try:
            dg = brain.team_digest(self.win.root, days=1)
            f, cur = dg["flow"], dg["current"]
            w = cur["wip"]
            tip = dg["suggestions"][0] if dg["risks"] and dg["suggestions"] else ""
            self.digestLine.setText(
                f"近 24h：记录 {f['records']} · 任务完成 {f['tasks_done']}"
                f" · 在办 {cur['open_total']}"
                f"（租约逾期 {w['stale_leases']} / 等审批 {w['approval_waiting']}）"
                f" · 风险 {len(dg['risks'])} 类" + (f"；{tip}" if tip else ""))
            self.digestLine.setToolTip("\n".join(f"- {s}" for s in dg["suggestions"]))
        except Exception as e:  # noqa: BLE001  日结失败不该拖垮工作台
            self.digestLine.setText(f"近 24h 日结读取失败：{e}")
        c = nxt["counts"]
        # 主四段恒显示（0 也显示，便于确认「真的没有」）；退避/延后只在有量时追加，
        # 避免常态下摘要在窄窗换行。R176 前这两组完全不出现在 GUI。
        parts = [f"{brain.TASK_GROUP_CN[k]} {c[k]}" for k in _GROUP_ORDER[:4]]
        parts += [f"{brain.TASK_GROUP_CN[k]} {c[k]}" for k in _GROUP_ORDER[4:] if c.get(k)]
        self.sumLabel.setText(" · ".join(parts))
        for key in _GROUP_ORDER:
            items = nxt.get(key) or []
            if not items:
                continue
            head = QListWidgetItem(f"── {brain.TASK_GROUP_CN[key]}（{len(items)}）")
            head.setFlags(Qt.ItemFlag.NoItemFlags)  # 分组标题不可选
            f = head.font()
            f.setBold(True)
            head.setFont(f)
            self.listw.addItem(head)
            for t in items:
                title = (t.get("title") or "").strip() or "(无标题)"
                proj = f"{t['project']}｜" if t.get("project") else ""
                meta = []
                if t.get("owner"):
                    meta.append(f"@{t['owner']}")
                if t.get("due"):
                    meta.append(f"截止:{t['due']}")
                if key == "waiting" and t.get("_wait"):
                    meta.append("等待:" + ",".join(f"#{w}" for w in self._wait_ids(t["_wait"])))
                if key == "takeover" and t.get("_takeover"):
                    meta.append(f"原因:{t['_takeover']}")
                if key == "backoff":
                    if t.get("_retry_wait_min"):
                        meta.append(f"{t['_retry_wait_min']} 分钟后可再试")
                    if t.get("attempts"):
                        meta.append(f"第 {t['attempts']} 次失败")
                if key == "deferred" and t.get("_defer_wait_h"):
                    meta.append(f"{t['_defer_wait_h']:g} 小时后到点")
                it = QListWidgetItem(
                    f"  #{t['id']} [P{t['_priority']}] {proj}{title}"
                    + (("  " + "  ".join(meta)) if meta else ""))
                it.setToolTip(self._task_tip(t, key, title))
                self._item2task[id(it)] = t
                self.listw.addItem(it)

    @staticmethod
    def _wait_ids(wait) -> list:
        """依赖等待可能是 id 列表，也可能是任务行 dict 列表（brain 两种历史形态）。"""
        return [str(w.get("id") if isinstance(w, dict) else w) for w in (wait or [])]

    @staticmethod
    def _task_tip(t: dict, key: str, title: str) -> str:
        """行内被省略/只放摘要的信息，在 tooltip 里给全：失败原因、退避到点、依赖等。"""
        lines = [f"#{t['id']} [P{t['_priority']}] {title}",
                 f"状态：{brain.TASK_CN.get(t.get('status', ''), t.get('status', ''))}"
                 f"（{brain.TASK_GROUP_CN.get(key, key)}）"]
        if t.get("project"):
            lines.append(f"项目：{t['project']}")
        if t.get("owner"):
            lines.append(f"负责人：{t['owner']}")
        if t.get("due"):
            lines.append(f"截止：{t['due']}")
        if key == "waiting" and t.get("_wait"):
            lines.append("等待依赖：" + "、".join(f"#{w}" for w in TodayPage._wait_ids(t["_wait"])))
        if key == "takeover" and t.get("_takeover"):
            lines.append(f"可接管原因：{t['_takeover']}")
        if key == "backoff":
            lines.append(f"退避剩余：{t.get('_retry_wait_min') or 0} 分钟"
                         + (f"（到 {t['next_retry_at']}）" if t.get("next_retry_at") else ""))
        if key == "deferred" and t.get("defer_until"):
            lines.append(f"延后到：{t['defer_until']}")
        if t.get("last_error"):
            lines.append(f"上次失败：{t['last_error']}")
        return "\n".join(lines)

    def _selected_task(self):
        it = self.listw.currentItem()
        return self._item2task.get(id(it)) if it else None

    # ---------- 操作 ----------
    def mark_done(self):
        t = self._selected_task()
        if not t:
            InfoBar.warning("先选中一个任务", "选中「进行中」段里的任务，再标记完成", parent=self)
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
