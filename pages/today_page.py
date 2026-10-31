# -*- coding: utf-8 -*-
"""TodayPage（M10·v2.30）：工作台——「今天该干什么」桌面视图。

hub_next 前沿队列的 GUI 呈现（同源 brain.task_next）：
  可认领（依赖已满足，按优先级 0急→3低 排序）/ 进行中 / 可接管（超 TTL 没人动）/ 等待依赖 四段。
只读看板 + 管理面操作（标记完成/取消/重试/调优先级/补详情与截止日都走 task_update
管理通道，done 触发 M2 自动结晶）。认领/改依赖走 MCP（hub_claim）——桌面端不代理 agent 身份。
"""
from __future__ import annotations

import re
from datetime import date

from PySide6.QtWidgets import QListWidgetItem  # noqa: F401  （uikit 的 * 不含它，显式带）

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （运行期重绑名动态取）
import brain


# 段序；中文名用 brain.TASK_GROUP_CN（同源）。
# R176：原只渲染前四段，退避/延后中的任务在 GUI 里凭空消失；六段都要可见。
_GROUP_ORDER = ("ready", "doing", "takeover", "waiting", "backoff", "deferred")

# R803：每组默认显示 20 条；超出时组头写「共 N，显示前 M」并给「显示更多」，
# 免得摘要里的总数与列表长度对不上、用户以为任务丢了。
_PAGE_DEFAULT = 20
_PAGE_STEP = 30
_PAGE_MAX = 200

# R793：brain.team_digest 的 suggestions 是写给 agent 的（带工具名），工作台直接展示
# 会漏出 hub_claim 这类天书。只在**展示层**换成人话，源数据保持 agent 可直接执行。
_TOOL_HUMAN = (
    (r"\s*\bhub_claim\b\s*认领", "认领"),
    (r"\s*\bhub_task_update\b\s*关单", "更新任务关单"),
    (r"\s*\bhub_task_add\b\s*新建任务", "新建任务"),
    (r"\bhub_task_retry\b", "重试任务"),
    (r"\bhub_heartbeat\b", "开工报到"),
    (r"\bhub_log_work\b", "记录工作"),
    (r"\bhub_memory_read\b", "查记忆"),
    (r"\bhub_memory_write\b", "写记忆"),
    (r"\bhub_report_error\b", "登记错误"),
    (r"\bhub_list_errors\b", "查看错误"),
    (r"\bhub_archive_project\b", "归档项目"),
    (r"\bhub_ops_run\b", "运行检查"),
    (r"\bhub_health\b", "体检"),
    (r"\bhub_distill\b", "蒸馏"),
    (r"\bhub_claim\b", "认领"),
    (r"\bhub_[a-z_]+\b", "对应操作"),
)


def _human_hint(text: str) -> str:
    for pattern, human in _TOOL_HUMAN:
        text = re.sub(pattern, human, text)
    return text


def _clip_reason(text: str, limit: int = 18) -> str:
    """外部阻塞原因的行内摘要（R806）：压掉换行、超长截断，全文始终留在 tooltip。"""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


class TodayPage(QWidget):
    """工作台：前沿队列四段一屏看全 + 管理通道标记完成。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self._item2task: dict = {}  # id(QListWidgetItem) -> task row（分组标题不进映射）
        lay = QVBoxLayout(self)
        set_page_margins(lay)
        lay.addWidget(_page_head(
            "工作台", "今天该干什么——可认领按优先级排队，进行中 / 可接管 / 等待依赖或外部阻塞 / 退避 / 延后分段列清；"
                      "标记完成走管理通道（自动归档为成果记录）"))

        # R807：任务一多，「找那一条」只能肉眼扫六段列表；顶部给一个即时筛选框，
        # 输入即过滤（标题/项目/#编号/负责人/阻塞原因），清空即还原。
        self.filterEdit = SearchLineEdit()
        self.filterEdit.setPlaceholderText("筛选任务：标题 / 项目 / #编号 / 负责人 / 阻塞原因")
        self.filterEdit.setToolTip("输入即筛选，多个关键词用空格分开（同时满足）；点右侧 × 或按 Esc 清空")
        self.filterEdit.textChanged.connect(lambda *_: self._fill())
        _esc = QShortcut(QKeySequence("Esc"), self.filterEdit)
        _esc.activated.connect(self.filterEdit.clear)
        lay.addWidget(self.filterEdit)

        top = QHBoxLayout()
        self.sumLabel = CaptionLabel("")
        self.sumLabel.setWordWrap(True)
        # 占满整行：空标签构造时的 sizeHint 很小，若只按 Preferred 加进 HBox，
        # 六段摘要会被截成前两段（R176 真实窗口实测）。
        top.addWidget(self.sumLabel, 1)
        # R803：截断时把「还有多少」摆到台面上；没有截断时按钮直接隐藏，空态不添噪音。
        self._page_limit = _PAGE_DEFAULT
        self.moreBtn = PushButton("显示更多")
        self.moreBtn.clicked.connect(self.load_more)
        self.moreBtn.hide()
        top.addWidget(self.moreBtn)
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
        self.retryBtn = PushButton("重试…")
        self.retryBtn.setToolTip("记录失败原因并放回队列（按失败次数指数退避）；"
                                 "进行中 / 阻塞 / 已退避的任务可用")
        self.retryBtn.clicked.connect(self.retry_task)
        self.cancelBtn = PushButton("取消任务…")
        self.cancelBtn.setToolTip("取消是终态，必须写明原因；保留任务供追溯，需要重做请新建")
        self.cancelBtn.clicked.connect(self.cancel_task)
        # R794：优先级原先只能让 agent 改，选中任务后桌面上却无从下手；
        # 这是一次纯管理面字段更新，不需要代理 agent 身份。
        self.prioBtn = PushButton("优先级")
        self.prioBtn.setToolTip("调整选中任务的优先级（P0 紧急 → P3 低），越急越先进入可认领队列")
        self.prioBtn.clicked.connect(self._show_priority_menu)
        # R799：交接时最先被问的两项——「这单要什么」和「什么时候要」——桌面原来改不了。
        self.detailBtn = PushButton("详情")
        self.detailBtn.setToolTip("查看并补充任务说明、调整截止日与前置依赖"
                                  "（状态与负责人仍由 agent 流转）")
        self.detailBtn.clicked.connect(self.edit_details)
        self.listw.currentItemChanged.connect(lambda *_: self._sync_actions())
        self.listw.itemDoubleClicked.connect(lambda *_: self.edit_details())
        row.addWidget(self.doneBtn)
        row.addWidget(self.retryBtn)
        row.addWidget(self.cancelBtn)
        row.addWidget(self.prioBtn)
        row.addWidget(self.detailBtn)
        # R799：加「详情」后行变宽；920 窗下内容区只有 749px，说明收短到能完整显示，
        # 边界解释挪进 tooltip（实测原句会被裁掉尾巴）。
        cap = CaptionLabel("认领由 agent 做")
        cap.setToolTip("桌面端不代理 agent 身份；对应工具：hub_next（看下一步）· "
                       "hub_claim（认领）· hub_task_add（新建）")
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
        # 取消：任何非终态都可（列表本来就只收非终态）。
        self.cancelBtn.setEnabled(bool(t) and t.get("status") not in brain.TASK_TERMINAL)
        # 重试：只有失败过（attempts>0）或正卡在进行中/阻塞才有意义；
        # 刚建出来的待办点「重试」只会凭空记一次失败。
        self.retryBtn.setEnabled(
            bool(t) and (int(t.get("attempts") or 0) > 0
                         or t.get("status") in ("in_progress", "blocked")))
        # R794：终态任务的优先级不再影响队列，别让按钮亮着骗人。
        self.prioBtn.setEnabled(bool(t) and t.get("status") not in brain.TASK_TERMINAL)
        # R799：详情/截止日同理——列表只收非终态，但选中态可能滞后于刷新。
        self.detailBtn.setEnabled(bool(t) and t.get("status") not in brain.TASK_TERMINAL)

    def _filter_tokens(self) -> list:
        """筛选词（R807）：空格分隔多词，全都要命中（AND），大小写不敏感。"""
        return [w for w in (self.filterEdit.text() or "").lower().split() if w]

    @staticmethod
    def _match(t: dict, tokens: list) -> bool:
        hay = " ".join(str(x or "") for x in (
            f"#{t.get('id')}", t.get("title"), t.get("project"), t.get("owner"),
            t.get("blocked_by"), t.get("detail"), t.get("deps"))).lower()
        return all(w in hay for w in tokens)

    def _fill(self):
        self.listw.clear()
        self._item2task.clear()
        self.moreBtn.hide()
        tokens = self._filter_tokens()
        loaded_total = 0
        self._match_total = 0
        if not self.win.root:
            self.sumLabel.setText("未配置数据根目录")
            self.digestLine.setText("")
            return
        try:
            nxt = brain.task_next(self.win.root, limit=self._page_limit)
        except Exception as e:  # noqa: BLE001  看板读失败不该拖垮窗口刷新
            self.sumLabel.setText(f"读取失败：{e}")
            return
        try:
            dg = brain.team_digest(self.win.root, days=1)
            f, cur = dg["flow"], dg["current"]
            w = cur["wip"]
            tip = _human_hint(dg["suggestions"][0]) \
                if dg["risks"] and dg["suggestions"] else ""
            self.digestLine.setText(
                f"近 24h：记录 {f['records']} · 任务完成 {f['tasks_done']}"
                f" · 在办 {cur['open_total']}"
                f"（租约逾期 {w['stale_leases']} / 等审批 {w['approval_waiting']}）"
                f" · 风险 {len(dg['risks'])} 类" + (f"；{tip}" if tip else ""))
            self.digestLine.setToolTip(
                "\n".join(f"- {_human_hint(s)}" for s in dg["suggestions"]))
        except Exception as e:  # noqa: BLE001  日结失败不该拖垮工作台
            self.digestLine.setText(f"近 24h 日结读取失败：{e}")
        c = nxt["counts"]
        # R807：先算好每段「加载了多少 / 命中多少」，摘要与分组标题才能说同一口径。
        loaded_total = sum(len(nxt.get(k) or []) for k in _GROUP_ORDER)
        visible = {}
        self._match_total = 0
        for k in _GROUP_ORDER:
            items = nxt.get(k) or []
            if tokens:
                items = [t for t in items if self._match(t, tokens)]
                self._match_total += len(items)
            visible[k] = items
        # 主四段恒显示（0 也显示，便于确认「真的没有」）；退避/延后只在有量时追加，
        # 避免常态下摘要在窄窗换行。R176 前这两组完全不出现在 GUI。
        parts = [self._group_count_label(k, c) for k in _GROUP_ORDER[:4]]
        parts += [f"{brain.TASK_GROUP_CN[k]} {c[k]}" for k in _GROUP_ORDER[4:] if c.get(k)]
        summary = " · ".join(parts)
        if tokens:
            total_all = sum(int(c.get(k) or 0) for k in _GROUP_ORDER)
            scope = (f"{self._match_total}/{loaded_total}（已加载）"
                     if total_all > loaded_total else str(self._match_total))
            summary += f" · 筛选命中 {scope}"
        self.sumLabel.setText(summary)
        hidden_n = 0
        for key in _GROUP_ORDER:
            all_items = nxt.get(key) or []
            if not all_items:
                continue
            total = int(c.get(key) or 0)
            shown = len(all_items)
            if total > shown:
                hidden_n += total - shown
            items = visible[key]
            if tokens:
                if not items:
                    continue
                label = f"匹配 {len(items)} · 共 {total}" + ("（仅查已加载）" if total > shown else "")
            else:
                label = f"{total}，显示前 {shown}" if total > shown else str(total)
            head = QListWidgetItem(f"── {self._group_head_name(key, items)}（{label}）")
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
                due_label = self._due_label(t)
                if due_label:
                    meta.append(due_label)
                if key == "waiting" and t.get("_wait"):
                    meta.append("等待:" + ",".join(f"#{w}" for w in self._wait_ids(t["_wait"])))
                elif key == "waiting" and (t.get("blocked_by") or "").strip():
                    # R806：外部阻塞的原因本来就在库里（「用户决策」），行内先给一眼，
                    # 全文留给 tooltip——原来这行只写「等待依赖」，等什么没人知道。
                    meta.append("等:" + _clip_reason(t["blocked_by"]))
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
        if tokens and loaded_total and not self._match_total:
            miss = QListWidgetItem(f"── 没有匹配「{self.filterEdit.text().strip()}」的任务"
                                   "——换关键词，或点右侧 × 清空筛选")
            miss.setFlags(Qt.ItemFlag.NoItemFlags)
            self.listw.addItem(miss)
        if hidden_n:
            capped = self._page_limit >= _PAGE_MAX
            self.moreBtn.setVisible(True)
            self.moreBtn.setEnabled(not capped)
            self.moreBtn.setToolTip(
                f"还有 {hidden_n} 条未显示；点此每组再加载 {_PAGE_STEP} 条"
                if not capped else
                f"还有 {hidden_n} 条未显示，已达单组 {_PAGE_MAX} 条上限")

    def load_more(self):
        """展开列表（R803）：每次每组多取 30 条，直到 200 条上限。"""
        if self._page_limit >= _PAGE_MAX:
            return
        self._page_limit = min(self._page_limit + _PAGE_STEP, _PAGE_MAX)
        self.reload()

    @staticmethod
    def _due_days(t: dict):
        """距截止还有几天（负数=已逾期）；空/坏日期返回 None，界面退回原样显示。"""
        due = (t.get("due") or "").strip()
        if not due:
            return None
        try:
            return (date.fromisoformat(due[:10]) - date.today()).days
        except ValueError:
            return None

    @classmethod
    def _due_label(cls, t: dict) -> str:
        """截止日的人话摘要（R800）：逾期/今天到期要一眼看见，未来日期保持原样。"""
        due = (t.get("due") or "").strip()
        if not due:
            return ""
        days = cls._due_days(t)
        if days is None:
            return f"截止:{due}"
        if days < 0:
            return f"逾期 {-days} 天（{due[:10]}）"
        if days == 0:
            return "今天到期"
        return f"截止:{due}"

    @staticmethod
    def _group_count_label(key: str, counts: dict) -> str:
        """页头摘要的一段（R806）：waiting 按成因分说，别把外部阻塞算成等待依赖。"""
        if key != "waiting":
            return f"{brain.TASK_GROUP_CN[key]} {counts.get(key) or 0}"
        deps = int(counts.get("waiting_deps") or 0)
        blocked = int(counts.get("waiting_blocked") or 0)
        if deps and blocked:
            return f"等待依赖 {deps} · 外部阻塞 {blocked}"
        if blocked:
            return f"外部阻塞 {blocked}"
        return f"{brain.TASK_GROUP_CN['waiting']} {deps}"

    @staticmethod
    def _group_head_name(key: str, items: list) -> str:
        """分组标题（R806）：waiting 段全是外部阻塞时，别写成「等待依赖」。"""
        name = brain.TASK_GROUP_CN.get(key, key)
        if key != "waiting" or not items:
            return name
        kinds = {(t.get("_wait_kind") or ("deps" if t.get("_wait") else "blocked"))
                 for t in items}
        if kinds == {"blocked"}:
            return "外部阻塞"
        if kinds == {"deps", "blocked"}:
            return "等待依赖 / 外部阻塞"
        return name

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
            due = (t.get("due") or "").strip()
            lines.append(f"截止：{due}")
            days = TodayPage._due_days(t)
            if days is not None and days < 0:
                lines.append(f"已逾期 {-days} 天")
            elif days == 0:
                lines.append("今天到期")
            lines.append("改期：选中任务 → 详情")
        if key == "waiting" and t.get("_wait"):
            lines.append("等待依赖：" + "、".join(f"#{w}" for w in TodayPage._wait_ids(t["_wait"])))
        elif t.get("deps"):
            lines.append("前置任务：" + "、".join(
                f"#{d.lstrip('#')}" for d in re.split(r"[,\s，、;；]+", t["deps"]) if d.strip()))
        if key == "waiting" and not t.get("_wait") and (t.get("blocked_by") or "").strip():
            lines.append("阻塞原因：" + t["blocked_by"].strip())
            lines.append("下一步：解开挡住它的那件事（等人拍板/等环境就绪），再让 agent 推进")
        if key == "takeover" and t.get("_takeover"):
            lines.append(f"可接管原因：{t['_takeover']}")
        if key == "backoff":
            lines.append(f"退避剩余：{t.get('_retry_wait_min') or 0} 分钟"
                         + (f"（到 {t['next_retry_at']}）" if t.get("next_retry_at") else ""))
        if key == "deferred" and t.get("defer_until"):
            lines.append(f"延后到：{t['defer_until']}")
        if t.get("last_error"):
            lines.append(f"上次失败：{t['last_error']}")
        if (t.get("detail") or "").strip():
            lines.append(f"详情：{t['detail'].strip()[:200]}")
        return "\n".join(lines)

    def _selected_task(self):
        it = self.listw.currentItem()
        return self._item2task.get(id(it)) if it else None

    def _show_priority_menu(self):
        t = self._selected_task()
        if not t:
            return
        cur = int(t.get("_priority", 2))
        menu = QMenu(self)
        chosen = None
        for val, label in NewTaskDialog.PRIORITIES:
            act = menu.addAction(("✓ " if val == cur else "   ") + f"P{val} {label}")
            act.setData(val)
            if val == cur:
                act.setEnabled(False)  # 当前档位不给点，避免「改了但没变」的假反馈
        chosen = menu.exec(self.prioBtn.mapToGlobal(self.prioBtn.rect().bottomLeft()))
        if chosen is not None:
            self.set_priority(chosen.data())

    def set_priority(self, priority: int):
        """改选中任务优先级（R794）：列表按 P0→P3 排队，改完立刻重排。"""
        t = self._selected_task()
        if not t:
            InfoBar.warning("先选中一个任务", "选中清单里的任务，再调整优先级", parent=self)
            return
        if t.get("status") in brain.TASK_TERMINAL:
            InfoBar.warning("状态不符", f"#{t['id']} 已是终态，优先级不再影响队列", parent=self)
            return
        val = max(0, min(int(priority), 3))
        if int(t.get("_priority", 2)) == val:
            return
        try:
            # agent 空传＝管理面：进行中任务可能由别的 agent 持有，带 "gui" 会被
            # 持有者校验挡掉（R794 实测），而桌面改优先级不该要求先接管任务。
            err, _row = brain.task_update(self.win.root, t["id"], priority=val, agent="")
        except Exception as e:  # noqa: BLE001
            InfoBar.error("优先级更新失败", str(e), parent=self)
            return
        if err:
            InfoBar.error("优先级更新失败", err, parent=self)
            return
        label = dict(NewTaskDialog.PRIORITIES).get(val, "")
        InfoBar.success("优先级已更新", f"#{t['id']} → P{val} {label}",
                        duration=2500, parent=self)
        self.reload()

    def edit_details(self):
        """打开详情对话框（R799）：选中任务的说明与截止日可改，其余只读。"""
        t = self._selected_task()
        if not t:
            InfoBar.warning("先选中一个任务", "选中清单里的任务，再打开详情", parent=self)
            return
        if t.get("status") in brain.TASK_TERMINAL:
            InfoBar.warning("状态不符", f"#{t['id']} 已是终态，详情不再修改", parent=self)
            return
        dlg = TaskDetailDialog(self.win, t)
        if not dlg.exec():
            return
        v = dlg.values()
        self.save_details(v["detail"], v["due"], v.get("deps"))

    def save_details(self, detail, due, deps=None):
        """保存详情/截止日/依赖（R799/R805）：只写变化的字段，清空落库成空（brain 认 "-"）。

        对话框与用例共用同一入口；空 agent＝管理面，别人持有的任务也能补说明。
        deps 传 None 表示不动依赖（兼容只改说明/截止日的调用）。
        """
        t = self._selected_task()
        if not t:
            InfoBar.warning("先选中一个任务", "选中清单里的任务，再补详情或截止日", parent=self)
            return
        if t.get("status") in brain.TASK_TERMINAL:
            InfoBar.warning("状态不符", f"#{t['id']} 已是终态，详情不再修改", parent=self)
            return
        detail = (detail or "").strip()
        due = (due or "").strip()
        deps = None if deps is None else (deps or "").strip()
        changed = {}
        if detail != (t.get("detail") or "").strip():
            changed["detail"] = detail or "-"
        if due != (t.get("due") or "").strip():
            changed["due"] = due or "-"
        if deps is not None and deps != (t.get("deps") or "").strip():
            changed["deps"] = deps or "-"
        if not changed:
            InfoBar.info("没有改动", f"#{t['id']} 的详情、截止日与依赖都没变",
                         duration=2000, parent=self)
            return
        try:
            err, _row = brain.task_update(self.win.root, t["id"], agent="", **changed)
        except Exception as e:  # noqa: BLE001
            InfoBar.error("保存失败", str(e), parent=self)
            return
        if err:
            InfoBar.error("保存失败", err, parent=self)
            return
        bits = []
        if "detail" in changed:
            bits.append("详情已更新" if detail else "详情已清空")
        if "due" in changed:
            bits.append(f"截止 {due}" if due else "截止日已清空")
        if "deps" in changed:
            bits.append(f"前置 {deps}" if deps else "前置依赖已清空")
        InfoBar.success("任务已更新", f"#{t['id']} " + "，".join(bits),
                        duration=2500, parent=self)
        self.reload()

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

    def _require_reason(self, title, hint, placeholder, confirm_text) -> str:
        dlg = ReasonDialog(self.win, title, hint, placeholder, confirm_text)
        if not dlg.exec():
            return ""
        return dlg.reason()

    def cancel_task(self, reason: str = ""):
        """取消选中任务（R792）：终态必须写原因；reason 为空走对话框。"""
        t = self._selected_task()
        if not t:
            InfoBar.warning("先选中一个任务", "选中清单里的任务，再取消", parent=self)
            return
        if t.get("status") in brain.TASK_TERMINAL:
            InfoBar.warning("状态不符", f"#{t['id']} 已是终态，不能取消", parent=self)
            return
        reason = reason or self._require_reason(
            f"取消任务 #{t['id']}",
            "取消是终态，必须写清原因或替代方案（复盘时能查到）",
            "例如：需求已撤，改由后续任务替代", "取消任务")
        if not reason:
            return
        try:
            err, _row = brain.task_update(self.win.root, t["id"], status="cancelled",
                                          agent="", cancel_reason=reason)
        except Exception as e:  # noqa: BLE001
            InfoBar.error("取消失败", str(e), parent=self)
            return
        if err:
            InfoBar.error("取消失败", err, parent=self)
            return
        InfoBar.success("任务已取消", f"#{t['id']} 已归档（保留记录，可追溯）",
                        duration=3000, parent=self)
        self.reload()

    def retry_task(self, reason: str = ""):
        """记录失败并退避（R792）：首次失败退避几分钟，次数耗尽自动进死信。"""
        t = self._selected_task()
        if not t:
            InfoBar.warning("先选中一个任务", "选中清单里的任务，再重试", parent=self)
            return
        reason = reason or self._require_reason(
            f"重试任务 #{t['id']}",
            "写清上次失败原因 / 这次怎么改，没依据的退避只会变成无限重试",
            "例如：网络超时，已确认代理后重试", "记录并重试")
        if not reason:
            return
        try:
            err, row = brain.task_retry(self.win.root, t["id"], agent="", reason=reason)
        except Exception as e:  # noqa: BLE001
            InfoBar.error("重试失败", str(e), parent=self)
            return
        if err:
            InfoBar.error("重试失败", err, parent=self)
            return
        if row.get("_dead"):
            InfoBar.warning("已进入死信",
                            f"#{t['id']} 连续失败 {row.get('_attempts')} 次，"
                            "已退出队列；需要再试请从这里复活", duration=4000, parent=self)
        else:
            mins = max(0, int(row.get("_backoff_seconds") or 0) // 60)
            wait = f"{mins} 分钟后可再认领" if mins else "已立即可再认领"
            InfoBar.success("已记录失败并退避", f"#{t['id']} {wait}",
                            duration=3000, parent=self)
        self.reload()
