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

        # 分区跳转条（R187）：长页 920×600 首屏只到「使用分布」，任务/待办/事件/置顶
        # 要盲滚才找得到；给一键锚点+各段条数，任务看板不必滚屏找。
        self._body = body
        self._section_heads: dict = {}
        self._jump_links: dict = {}
        self._jump_names = {"top": "唤起排行", "use": "使用分布", "task": "任务看板",
                            "todo": "待办看板", "event": "事件流", "pin": "置顶记忆"}
        jumpRow = FlowLayout()
        jumpRow.addWidget(CaptionLabel("跳到"))
        for key, text in self._jump_names.items():
            lb = ClickBodyLabel(text)
            lb.setStyleSheet(f"color:{tokens.C(tokens.ACCENT)};font-size:12px;")
            lb.setToolTip(f"跳到「{text}」")
            lb.clicked.connect(lambda k=key: self._jump_to(k))
            jumpRow.addWidget(lb)
            self._jump_links[key] = lb
        lay.addLayout(jumpRow)

        # 随记已独立成页（NotesPage——人的模块容器），大脑页回归 agent 数据

        self.topBox = QVBoxLayout()
        self.useBox = QVBoxLayout()
        self.taskBox = QVBoxLayout()
        self.todoBox = QVBoxLayout()
        self.eventBox = QVBoxLayout()
        self.pinBox = QVBoxLayout()
        self.topBox.setSpacing(tokens.SPACE_S)
        self._top_expanded = False
        # 唤起排行默认收 5 条：920×600 实测 10 条占约 250px，把「使用分布/任务看板」
        # 整段推到首屏之外；前 5 条足以判断推送质量，需要时一键展开全部。
        lay.addSpacing(16)
        _topHead = QHBoxLayout()
        _topHeadWidget = section_head("记忆唤起排行（开工推送反射弧记数）", "VIEW")
        self._section_heads["top"] = _topHeadWidget
        _topHead.addWidget(_topHeadWidget, 1)  # stretch 给标题：guard_overflow 把它设 Ignored 后，
        # 无 stretch 的行只会按 0 宽分配（实测标题只剩 6 个字）；stretch 让它吃满剩余宽度。
        self.topToggle = ClickBodyLabel("")
        self.topToggle.setStyleSheet(f"color:{tokens.C(tokens.ACCENT)};font-size:12px;")
        self.topToggle.setToolTip("默认显示前 5 条；展开后可看完整 top10")
        self.topToggle.clicked.connect(self._toggle_top)
        _topHead.addWidget(self.topToggle)
        lay.addLayout(_topHead)
        lay.addLayout(self.topBox)
        # 内容组不包卡：间距+小标题分组（Fluent Layout——分组靠 spacing，不需要每块加框）
        for key, box, t, icn in (("use", self.useBox, "大脑使用分布（主动检索 + 推送接收，按 agent）", "PEOPLE"),
                                 ("task", self.taskBox, "任务看板（agent 经公用大脑建任务、认领，进行中→阻塞→待办→完成）", "CERTIFICATE"),
                                 ("todo", self.todoBox, "待办看板（记录里提取的欠账，处理后勾销闭环）", "CHECKBOX"),
                                 ("event", self.eventBox, "事件流（agent 经 hub_event_write 写进展，"
                                                          "hub_events 增量订阅）", "MESSAGE"),
                                 ("pin", self.pinBox, "置顶记忆（置顶位只放工作知识）", "PIN")):
            box.setSpacing(tokens.SPACE_S)  # 行距：无间距时唤起/置顶行挤成一片
            lay.addSpacing(16)  # 组间距 > 行距，拉开层级
            head = section_head(t, icn)
            self._section_heads[key] = head
            lay.addWidget(head)
            lay.addLayout(box)
        lay.addStretch(1)
        self.scroll.setWidget(body)
        self.scroll.enableTransparentBackground()  # 必须在 setWidget 后（qfw API 需 widget 已存在才命中 body；2026-10-10 深底错配根因）
        outer.addWidget(self.scroll, 1)

    def reload(self, force: bool = False):
        root = self.win.root
        if not root:
            return
        try:
            h = brain.health_report(root)
        except Exception as e:
            # R143：读失败时页头给错误而非静默空白（用户分不清「没数据」和「坏了」）
            self.valMem.setText("—")
            self.valCrys.setText("—")
            self.valPush.setText("—")
            self.valTodo.setText("—")
            InfoBar.error("大脑体检读取失败", f"{type(e).__name__}: {str(e)[:80]}",
                         duration=6000, parent=self.win)
            return
        # R162：数据指纹跳过——qfw 组件整页重建在长跑下原生内存线性涨（老化实测
        # 30 轮 +256MB、60 轮 +505MB 完美线性；对照纯 QLabel 仅 ~4.5KB/个 vs
        # qfw 组件 ~40KB/个——第三方库固有分配行为）。真实使用中 F5 连点/切页触发
        # reload 远多于数据实际变化：指纹（报告内容的稳定序列化）相同就跳过整页
        # 重建；数据变了才真正重画。health_report 本身无泄漏（×30 收敛 +9MB）。
        import json as _json
        try:
            fp = _json.dumps(h, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 有不可序列化字段就退回总是重建
            fp = ""
        if not force and fp and getattr(self, "_data_fp", None) == fp:
            return
        self._data_fp = fp
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
                    # R161：布局对象自身也是 QObject——每轮 addLayout 的新行布局不销毁
                    # 就线性累积（老化实测 30 轮 +256MB，锁定本页；widget 已拆但 layout
                    # 壳与 spacer item 永生）
                    sub.deleteLater()

        # 唤起排行（验收①：推送 top 应是工作知识而非元信息）
        clear(self.topBox)
        tops = h.get("top_pushed", [])
        shown = tops if self._top_expanded else tops[:5]
        for m in shown:
            self.topBox.addWidget(self._mem_row(f"#{m['id']} ×{m['use_count']}", m))
        self.topToggle.setVisible(len(tops) > 5)
        self.topToggle.setText(
            "收起" if self._top_expanded else f"展开全部 {len(tops)} 条")
        if not tops:
            self.topBox.addWidget(empty_state(
                "HISTORY", "还没有记忆被唤起",
                "agent 每次开工报到时会自动推送相关记忆；这里累计被推送的次数"))

        # 使用分布（验收①：searches 有跨 agent 真实数据）
        clear(self.useBox)
        merged: dict = {}
        for src in (h.get("search_by_agent", {}), h.get("push_by_agent", {})):
            for k, v in src.items():
                merged[k] = merged.get(k, 0) + v
        total = max(1, sum(merged.values()))
        # R121：probe（闸门探针会话）是系统自检流量不是「谁在用大脑」——混进排名
        # 会挤掉真 agent 的位置（真实库实测 probe 出现在榜上）；置底并如实标注。
        rows = sorted(merged.items(), key=lambda x: -x[1])
        rows.sort(key=lambda kv: 1 if kv[0] == "probe" else 0)
        for k, v in rows:
            r = QHBoxLayout()
            r.addWidget(badge(k, k))
            bar = ProgressBar()
            bar.setValue(round(v / total * 100))
            r.addWidget(bar, 1)
            name = f"{v}（探针，非真实使用）" if k == "probe" else str(v)
            r.addWidget(BodyLabel(name))
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
                "agent 建任务并认领后，这里显示持有者与阻塞状态"))

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
                "agent 写下进展后，其他 agent 可以按增量订阅这些事件"))

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

        counts = {"top": len(tops), "use": len(merged), "task": len(tasks),
                  "todo": len(h["todos"]), "event": len(events), "pin": len(pins)}
        for key, lb in self._jump_links.items():
            name = self._jump_names[key]
            n = counts.get(key, 0)
            lb.setText(f"{name} {n}" if n else name)
            lb.setToolTip(f"跳到「{name}」" + (f"（当前显示 {n} 条）" if n else ""))

    def _jump_to(self, key: str) -> None:
        head = self._section_heads.get(key)
        if head is None:
            return
        bar = self.scroll.verticalScrollBar()
        y = head.mapTo(self._body, QPoint(0, 0)).y() - tokens.SPACE_M
        bar.setValue(max(bar.minimum(), min(bar.maximum(), y)))

    def _toggle_top(self):
        self._top_expanded = not self._top_expanded
        self.reload(force=True)

    @staticmethod
    def _mem_row(head: str, m: dict) -> QWidget:
        """唤起/置顶行：类型徽章 + 编号·次数 + 正文单行截断，全文进 tooltip。
        两端平衡（R120）：最早是灰小字 [:66]「看不清」→ 改 [:100] 可换行后 top10
        每条 2-3 行、列表占满半屏（真实库走查实测）。单行 [:72] + 悬停看全文。"""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(tokens.SPACE_S)
        h.addWidget(kind_badge(m["kind"]))
        # 920×600 实测：固定取 72 字仍会超出可用宽度，QLabel 直接硬切（无省略号，
        # 行尾停在半个词上）。改用按实际宽度省略的标签，全文仍进 tooltip。
        body = ClickBodyLabel(f"{head}  {m['content'][:72]}")
        body.setWordWrap(False)
        body.setToolTip(f"{head}\n{m['content']}")
        h.addWidget(flex_label(body), 1)
        return w

    def _done_todo(self, rid, todo):
        err = brain.mark_todo_done(self.win.root, rid, todo, agent="gui")
        if err:
            InfoBar.error("勾销失败", err, duration=4000, parent=self.win)
            return
        InfoBar.success("已勾销", "该待办不再出现（闭环），操作可在流水页追溯", duration=3000, parent=self.win)
        self.reload()

