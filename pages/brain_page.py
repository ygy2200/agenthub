# -*- coding: utf-8 -*-
"""BrainPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
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
        set_page_margins(lay)
        lay.addWidget(_page_head("大脑", "公用记忆的运转状态——唤起排行 / 使用分布 / 任务看板 / 待办线索 / 事件流 / 置顶记忆"))
        # 健康读数：纯展示不包卡（卡片语义留给可点区域），大数字+小标题直接排版
        row = FlowLayout()   # 窄窗换行（原 QHBoxLayout 只会等比压扁）
        self.valMem, self.valCrys, self.valPush, self.valTodo = (TitleLabel("0") for _ in range(4))
        # 数字走 metric（32px 定宽）——原为 TitleLabel 28，与页面标题同字号、层级不分
        for _v in (self.valMem, self.valCrys, self.valPush, self.valTodo):
            tokens.typeset(_v, "metric")
        for val, t in ((self.valMem, "记忆总数"), (self.valCrys, "结晶率（记忆/记录）"),
                       (self.valPush, "记忆推送累计"), (self.valTodo, "待办线索")):
            col = QVBoxLayout()
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(tokens.SPACE_XS)
            col.addWidget(val)
            col.addWidget(CaptionLabel(t))
            box = QWidget()
            box.setLayout(col)
            row.addWidget(box)
        lay.addLayout(row)

        # 随记已独立成页（NotesPage——人的模块容器），大脑页回归 agent 数据

        self.topBox = QVBoxLayout()
        self.useBox = QVBoxLayout()
        self.taskBox = QVBoxLayout()
        self.todoBox = QVBoxLayout()
        self.eventBox = QVBoxLayout()
        self.pinBox = QVBoxLayout()
        # 内容组不包卡：间距+小标题分组（Fluent Layout——分组靠 spacing，不需要每块加框）
        for box, t, icn in ((self.topBox, "记忆唤起 top10（开工推送反射弧记数）", "VIEW"),
                            (self.useBox, "大脑使用分布（主动检索 + 推送接收，按 agent）", "PEOPLE"),
                            (self.taskBox, "任务看板（P3-2 状态机：agent 经 hub_task_add 建、hub_claim 认领，"
                                           "进行中→阻塞→待办→完成）", "CERTIFICATE"),
                            (self.todoBox, "待办看板（记录里提取的欠账，处理后勾销闭环）", "CHECKBOX"),
                            (self.eventBox, "事件流（agent 经 hub_event_write 写进展，"
                                            "hub_events 增量订阅）", "MESSAGE"),
                            (self.pinBox, "置顶记忆（置顶位只放工作知识）", "PIN")):
            box.setSpacing(tokens.SPACE_S)  # 行距：无间距时唤起/置顶行挤成一片
            lay.addSpacing(16)  # 组间距 > 行距，拉开层级
            lay.addWidget(section_head(t, icn))
            lay.addLayout(box)
        lay.addStretch(1)
        self.scroll.setWidget(body)
        self.scroll.enableTransparentBackground()  # 必须在 setWidget 后（qfw API 需 widget 已存在才命中 body；2026-10-10 深底错配根因）
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
        self.valMem.setText(str(h["memories"]))
        self.valCrys.setText(f"{h['crystallization']}%")
        self.valPush.setText(str(h.get("recall_push_total", 0)))
        self.valTodo.setText(str(h["todo_count"]))

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
            self.topBox.addWidget(empty_state(
                "HISTORY", "还没有记忆被唤起",
                "agent 开工心跳（hub_heartbeat）时自动推送相关记忆，此处累计记数"))

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
            self.useBox.addWidget(empty_state(
                "TILES", "暂无检索/推送记录",
                "agent 主动检索或接收开工推送后，这里按家分布累计"))

        # 任务看板（P3-2 状态机视图）：状态胶囊 + 认领人 + 阻塞信息
        clear(self.taskBox)
        try:
            # R14 与 MCP/CLI 同口径：注解一次带出任务租约状态、截止紧迫度、文件锁计数；
            # 此前 GUI 自己重算租约/逾期，文件锁则完全看不见（控制面在桌面断了一截）。
            tasks = brain.task_annotate(root, brain.task_list(root, limit=12))
        except Exception:
            tasks = []
        try:
            leases = brain.file_lease_list(root, include_released=False, limit=200)
        except Exception:
            leases = []
        chip_key = {"in_progress": "accent", "blocked": "warning", "done": "success",
                    "dead": "warning", "cancelled": "text_secondary"}
        lease_label = {"expired": "租约过期", "ownerless": "无主", "unleased": "缺租约"}
        lease_tip = {
            "expired": lambda t: (f"认领于 {t['claimed_at'][:16]}，超过 "
                                  f"{brain.CLAIM_TTL_HOURS}h 未续租——可被 hub_claim 接管"),
            "ownerless": lambda t: "在办但无持有者——可被 hub_claim 接管",
            "unleased": lambda t: "在办但缺认领时间——可被 hub_claim 接管",
        }
        for t in tasks:
            r = QHBoxLayout()
            r.addWidget(status_chip(brain.TASK_CN.get(t["status"], t["status"]),
                                    chip_key.get(t["status"], "text_secondary")))
            # R7 任务治理可见性：过期/无主/缺租约直接标在桌面，不用去跑 hub_ops_run
            state = t.get("_lease_state") or ""
            if state in lease_label:
                chip = status_chip(lease_label[state], "warning")
                chip.setToolTip(lease_tip[state](t))
                r.addWidget(chip)
            if t.get("_due_state") == "overdue":
                due = (t.get("due") or "").strip()
                days = t.get("_due_days")
                chip = status_chip(f"逾期 {due[5:10]}", "warning")
                chip.setToolTip(f"截止 {due} 已过 {abs(days) if days is not None else '?'} 天；"
                                "改期用 hub_task_update(due=...)")
                r.addWidget(chip)
            # R26 进展停滞：心跳在线不等于任务在走——只看租约的桌面会漏掉「人在、活停」
            if t.get("_progress_stall"):
                age = t.get("_progress_age_h")
                chip = status_chip(f"进展停滞 {age:g}h" if isinstance(age, (int, float))
                                   else "进展停滞", "warning")
                chip.setToolTip(
                    f"超过 {brain.TASK_PROGRESS_STALL_HOURS}h 没有检查点/任务更新"
                    "（心跳不计入推进）；写进展用 hub_checkpoint(action=save)")
                r.addWidget(chip)
            # R30 待办饥饿：能开工却长期没人认领——空转的活也要在桌面上看见
            if t.get("_starve_days") is not None:
                chip = status_chip(f"待办饥饿 {t['_starve_days']:g} 天", "warning")
                chip.setToolTip(
                    f"任务创建后 {t['_starve_days']:g} 天一直可直接开工，却无人认领；"
                    "还该做就 hub_claim，不做就 hub_task_update 调整优先级或关单")
                r.addWidget(chip)
            # R42-R45 过程控制：死信/退避/延后/审批四类状态桌面直接可见
            if t.get("status") == "dead":
                chip = status_chip(f"死信（{t.get('_attempts') or 0} 次失败）", "warning")
                chip.setToolTip("重试耗尽已退出队列；排查后 hub_task_retry 复活（次数重置），"
                                "或确认放弃后新建任务")
                r.addWidget(chip)
            elif t.get("_retry_wait_min"):
                chip = status_chip(f"退避 {t['_retry_wait_min']} 分钟", "warning")
                chip.setToolTip(
                    f"第 {t.get('_attempts') or 0} 次失败后指数退避；"
                    f"原因：{(t.get('last_error') or '-')[:60]}；"
                    "确认修复后 hub_task_retry(delay=0) 提前放行")
                r.addWidget(chip)
            if t.get("_defer_wait_h"):
                until = (t.get("defer_until") or "")[:16].replace("T", " ")
                chip = status_chip(f"延后至 {until[5:]}", "text_secondary")
                chip.setToolTip(
                    f"原因：{(t.get('defer_reason') or '-')[:80]}；到点自动回到前沿，"
                    "提前唤醒 hub_task_update(defer_until='-')")
                r.addWidget(chip)
            if t.get("_approval_required"):
                if (t.get("approved_by") or "").strip():
                    chip = status_chip(f"已审批 {t['approved_by']}", "success")
                    chip.setToolTip("已由他人复核批准；持有者可标 done")
                else:
                    wait_h = t.get("_approval_wait_h")
                    if isinstance(wait_h, (int, float)):
                        aging = bool(t.get("_approval_aging"))
                        chip = status_chip(f"待批 {wait_h:g}h",
                                           "warning" if aging else "text_secondary")
                        chip.setToolTip(
                            (f"待批过久：已等 {wait_h:g}h（阈值 {brain.TASK_APPROVAL_AGING_H}h）——"
                             if aging else f"已等批 {wait_h:g}h——")
                            + "done 前需他人/管理面 hub_task_approve 批准（owner 不能自批）")
                    else:
                        chip = status_chip("待审批", "warning")
                        chip.setToolTip("done 前需他人/管理面 hub_task_approve 批准（owner 不能自批）")
                r.addWidget(chip)
            # R14 文件租约可见性：挂在本任务上的活跃锁（与 hub_tasks 行尾「锁 N」同口径）
            locks = t.get("_locks") or 0
            if locks:
                chip = status_chip(f"文件锁 {locks}", "accent")
                mine = [x for x in leases
                        if x.get("task_id") == t["id"] and x.get("_active")]
                detail = "；".join(
                    f"{x['display_path'] or x['path']} ← {x['owner'] or '无主'}"
                    f"（剩 {x.get('_left_min', 0)}m）" for x in mine[:5])
                chip.setToolTip("挂在本任务上的文件租约："
                                + (detail or f"{locks} 把有效锁"))
                r.addWidget(chip)
            who = f" @{t['owner']}" if t["owner"] else ""
            blk = (f"（等待 {t['blocked_by'][:20]}）"
                   if t["status"] == "blocked" and t["blocked_by"] else "")
            proj = f"[{t['project']}] " if t["project"] else ""
            r.addWidget(BodyLabel(f"#{t['id']} {proj}{t['title'][:60]}{who}{blk}"), 1)
            self.taskBox.addLayout(r)
        if not tasks:
            self.taskBox.addWidget(empty_state(
                "ACCEPT", "暂无任务",
                "agent 经 hub_task_add 建任务、hub_claim 认领；认领后此处显示持有者与阻塞状态"))

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
            self.todoBox.addWidget(empty_state(
                "ACCEPT", "暂无待办",
                "从记录里自动提取「后续 / 待验证 / 下一步…」句式；在时间线勾销即闭环"))

        # 事件流（P3-2）：agent 协作通道的 GUI 可观测面（写进展 / 增量订阅）
        clear(self.eventBox)
        try:
            events = brain.event_list(root, limit=8)
        except Exception:
            events = []
        for e in events:
            proj = f"·{e['project']}" if e["project"] else ""
            self.eventBox.addWidget(BodyLabel(
                f"#{e['id']} {e['ts'][5:16]} [{e['agent'] or '-'}{proj}] {e['content'][:72]}"))
        if not events:
            self.eventBox.addWidget(empty_state(
                "HISTORY", "暂无事件",
                "agent 用 hub_event_write 写进展，其他 agent 用 hub_events 增量订阅"))

        # 置顶记忆（验收④：置顶位工作知识占比）
        clear(self.pinBox)
        try:
            pins = [m for m in brain.search_memories(root, "", "", 100) if m["pinned"]][:8]
        except Exception:
            pins = []
        for m in pins:
            self.pinBox.addWidget(self._mem_row(f"★ #{m['id']}", m))
        if not pins:
            self.pinBox.addWidget(empty_state(
                "PIN", "暂无置顶",
                "agent 写记忆时可带 pinned=true 进置顶位——每次读记忆优先展示"))

    @staticmethod
    def _mem_row(head: str, m: dict) -> QWidget:
        """唤起/置顶行：类型徽章 + 编号·次数 + 正文（正常字号可换行）。
        原来是单行 CaptionLabel 灰小字且 [:66]/[:70] 截断，是"看不清"的另一半根因。"""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(tokens.SPACE_S)
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

