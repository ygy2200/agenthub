# -*- coding: utf-8 -*-
"""StatsPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
class StatsPage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
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
        lay.addWidget(_page_head("统计", "数据全貌与大脑体检——项目 / 记录 / 对账 / 停滞 / 检索"))
        # 健康读数：纯展示不包卡，大数字+小标题直接排版
        row = FlowLayout()   # 窄窗 5→3→2 换行（原 QHBoxLayout 等比压扁）
        self.valProj, self.valRec, self.valIssue, self.valStalled, self.valSearch = \
            (TitleLabel("0") for _ in range(5))
        for _v in (self.valProj, self.valRec, self.valIssue, self.valStalled, self.valSearch):
            tokens.typeset(_v, "metric")   # 数字统一 32px 定宽（与总览数字卡同档）
        for val, t in ((self.valProj, "项目总数"), (self.valRec, "工作记录条数"),
                       (self.valIssue, "对账问题数"), (self.valStalled, "停滞项目（90天）"),
                       (self.valSearch, "检索（今日/累计）")):
            col = QVBoxLayout()
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(tokens.SPACE_XS)
            col.addWidget(val)
            col.addWidget(CaptionLabel(t))
            box = QWidget()
            box.setLayout(col)
            row.addWidget(box)
        lay.addLayout(row)

        self.agentBox = QVBoxLayout()
        self.monthBox = QVBoxLayout()
        self.healthLine = CaptionLabel("")
        self.healthLine.setWordWrap(True)
        self.taskLine = CaptionLabel("")
        self.taskLine.setWordWrap(True)
        # R786：任务过程原是一整段「新建 X · 完成 Y（中位/P90）；在办 Z：租约超时/无主/等审批/逾期；
        # 重试存量…；风险…（CLI：tasks --stats）」——六个维度挤在一行里，扫读成本高。
        # 拆成指标格子：一句人话摘要 + 可扫读的方格，命令行口径收进 tooltip。
        self.taskMetrics = QWidget()
        self._taskFlow = FlowLayout(self.taskMetrics)
        self._taskFlow.setContentsMargins(0, 0, 0, 0)
        self._taskFlow.setSpacing(tokens.SPACE_M)
        self._task_chips = {}
        # 可行动摘要前置：920×600 最小窗下，先看到「哪里不对/任务是否在走」，
        # 再看 agent/月度分布这类回溯性细节（分布区仍完整保留在下方滚动区）。
        lay.addSpacing(16)
        lay.addWidget(section_head("大脑体检（智能摘要）", "HEART"))
        lay.addWidget(self.healthLine)
        lay.addSpacing(16)
        lay.addWidget(section_head("任务过程（近 7 天）", "HISTORY"))
        lay.addWidget(self.taskLine)
        lay.addWidget(self.taskMetrics)
        # 内容组不包卡：间距+小标题分组
        for box, t, icn in ((self.agentBox, "各 agent 记录分布", "PEOPLE"),
                            (self.monthBox, "近 6 个月活跃", "CALENDAR")):
            lay.addSpacing(16)
            lay.addWidget(section_head(t, icn))
            lay.addLayout(box)
        lay.addStretch(1)

    # ------------------------------------------------------------ 取数 / 渲染分离（R239）
    # 真实库实测（output/_r239_dupes.txt）：这一页 `_reload_db_page` 墙钟 383ms，其中
    # 建控件/排版只有 38ms，其余 345ms 全在等数据库——similar_memories 132ms、
    # distill_candidates 78ms、health_report、task_metrics（还调了两次 stats）。
    # 所以取数单独放 fetch_db（纯 brain，可在 FnWorker 后台线程跑），渲染走 apply_db
    # （只碰控件，必须主线程）。原 `set_db` 里藏着三次 brain 调用，正是「看着像渲染
    # 其实在等库」的元凶，一并拆出来。

    def fetch_db(self, root: str) -> dict:
        """读这一页要的全部数据——不在主线程，禁止在这里碰任何控件。"""
        st = brain.stats(root)
        return {"stats": st,
                "health": _read_health(root, st),
                "distill": _read_distill(root),
                "tasks": _read_tasks(root)}

    def set_db(self, s: dict):
        """同步渲染入口（smoke/走查工具/旧调用方在用）：补齐剩下的数据再渲染。"""
        root = self.win.root
        self.apply_db({"stats": s, "health": _read_health(root, s),
                       "distill": _read_distill(root), "tasks": _read_tasks(root)})

    def apply_db(self, bundle: dict):
        """统计页渲染：拿已经取好的数据填控件，一次数据库都不碰。"""
        self._hide_overlay()
        s = bundle.get("stats") or {}
        # R131：千分位——1512 与 1,512 一眼可辨（数字卡走 32px 定宽，逗号占位无害）
        fmt = lambda n: f"{int(n or 0):,}"
        self.valProj.setText(fmt(s.get("projects", 0)))
        self.valRec.setText(fmt(s.get("records", 0)))
        self.valIssue.setText(fmt(s.get("errors_open", 0)))
        self.valStalled.setText(fmt(s.get("projects_stalled", 0)))
        self.valSearch.setText(f"{fmt(s.get('searches_today', 0))} / {fmt(s.get('searches_total', 0))}")
        h = bundle.get("health")
        if h is None:
            self.healthLine.setText("体检暂不可用")
        else:
            dup = f"疑似重复记忆 {h['dup_memory_count']} 组"
            todo = f"待办线索 {h['todo_count']} 条"
            tip = []
            if h['dup_memory_count']:
                tip.append(dup)
            if h['todo_count']:
                tip.append(todo)
            if h['projects_stalled']:
                tip.append(f"停滞项目 {h['projects_stalled']} 个")
            dc = bundle.get("distill") or 0
            if dc:
                tip.append(f"蒸馏候选 {dc} 条")
            crys = ("—" if h["crystallization"] is None else f"{h['crystallization']}%")
            self.healthLine.setText(
                f"结晶率 {crys}（记忆/记录）· 检索 今日 {h['searches_today']} / 累计 {h['searches_total']} 次"
                + (" · " + " · ".join(tip) if tip else " · 各项整洁")
                + "（agent 可调 hub_health / hub_distill 看完整报告）")
        m = bundle.get("tasks")
        if m is None:
            self.taskLine.setText("任务统计暂不可用")
            self._fill_task_metrics(None)
        else:
            f, w, r = m["flow"], m["wip"], m["retry"]
            risks = m.get("risks") or []
            self.taskLine.setText(
                f"近 7 天新建 {f['created']} 条、完成 {f['done']} 条；"
                f"当前在办 {w['in_progress']} 条，风险 {len(risks) or '无'} 项")
            self.taskLine.setToolTip(
                "命令行同口径：python cli.py tasks --stats"
                "（近 7 天新建/完成/取消、完成周期中位与 P90、在办构成、重试存量、风险信号）")
            self._fill_task_metrics(m)


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

        clear(self.agentBox)
        total = max(1, s.get("records", 0))
        for agent, n in sorted(s.get("agent_counts", {}).items(), key=lambda x: -x[1]):
            row = QHBoxLayout()
            row.addWidget(badge(agent, agent))
            bar = ProgressBar()
            bar.setValue(round(n / total * 100))
            row.addWidget(bar, 1)
            row.addWidget(BodyLabel(str(n)))
            self.agentBox.addLayout(row)
        if not s.get("agent_counts"):
            self.agentBox.addWidget(empty_state(
                "PEOPLE", "暂无数据", "agent 写入工作记录后，这里按家分布"))

        clear(self.monthBox)
        months = sorted(s.get("monthly_counts", {}).items())[-6:]
        mx = max((v for _, v in months), default=1)
        for m, n in months:
            row = QHBoxLayout()
            row.addWidget(BodyLabel(m))
            bar = ProgressBar()
            bar.setValue(round(n / mx * 100))
            row.addWidget(bar, 1)
            row.addWidget(BodyLabel(str(n)))
            self.monthBox.addLayout(row)
        if not months:
            self.monthBox.addWidget(empty_state(
                "CALENDAR", "暂无数据", "记录产生后，这里显示近 6 个月活跃度"))

    def _fill_task_metrics(self, m):
        """任务过程指标格（R786）：先清后填；tone 只给需要警觉的格子。

        m=None（统计不可用）时留一行「—」，避免上一次的数字冒充这一次。
        """
        while self._taskFlow.count():
            it = self._taskFlow.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._task_chips = {}
        if m is None:
            return
        f, w, r = m["flow"], m["wip"], m["retry"]
        dc = f["done_cycle_hours"] or {}

        def hh(x):
            return "—" if x is None else f"{x:g}h"

        risks = m.get("risks") or []
        labels = {"lease_stale": "认领超时", "unowned": "无主", "approval_waiting": "等审批",
                  "overdue": "逾期", "dead_letter": "死信", "retry_storm": "重试≥2"}
        risk_text = "；".join(f"{labels.get(x['kind'], x['kind'])} {x['count']}" for x in risks) or "无"
        spec = (
            ("近 7 天新建", str(f["created"]), ""),
            ("近 7 天完成", str(f["done"]), ""),
            ("完成周期·中位", hh(dc.get("p50")), ""),
            ("完成周期·慢 10%", hh(dc.get("p90")), ""),
            ("在办", str(w["in_progress"]), ""),
            ("认领超时", str(w["stale_leases"]), tokens.WARNING if w["stale_leases"] else ""),
            ("等审批", str(w["approval_waiting"]), tokens.WARNING if w["approval_waiting"] else ""),
            ("逾期", str(w["overdue"]), tokens.DANGER if w["overdue"] else ""),
            ("重试过的任务", str(r["tasks_with_attempts"]), ""),
            ("重试总次数", str(r["attempts_total"]), ""),
            ("风险", risk_text, tokens.DANGER if risks else ""),
        )
        tips = {"完成周期·中位": "一半任务在这个时间内完成",
                "完成周期·慢 10%": "九成任务在这个时间内完成（P90）",
                "认领超时": "认领后长时间没有新进展，租约已超时",
                "重试过的任务": "失败后重试过至少一次的任务数"}
        for label, value, tone in spec:
            chip = MetricChip(label, value, tone, self.taskMetrics)
            if label in tips:
                chip.setToolTip(tips[label])
            self._task_chips[label] = chip
            self._taskFlow.addWidget(chip)

    # ---------------------------------------------------------------- 忙态 / 失败占位

    def show_busy(self):
        """取数在飞时盖住整页的骨架屏：宁可有东西在动，也不给一屏「0」。"""
        self._overlay("busy")

    def show_failure(self, err) -> None:
        """取数失败：说清楚是「读不出来」而不是「库里没东西」，并保留重试责任。"""
        self._overlay("fail", str(err))

    def _overlay(self, kind: str, text: str = "") -> None:
        box = getattr(self, "_ov", None)
        if box is None:
            box = QWidget(self)
            box.setAttribute(Qt.WA_StyledBackground, True)
            box.setStyleSheet(f"background:{tokens.resolve('bg_page')};")
            lay = QVBoxLayout(box)
            lay.setContentsMargins(tokens.SPACE_XL, tokens.SPACE_XL, tokens.SPACE_XL, tokens.SPACE_XL)
            lay.setSpacing(tokens.SPACE_M)
            self._ov_sk = Skeleton(rows=4, parent=box)
            lay.addWidget(self._ov_sk)
            self._ov_tx = CaptionLabel("", box)
            self._ov_tx.setWordWrap(True)
            lay.addWidget(self._ov_tx)
            lay.addStretch(1)
            self._ov = box
        box.setGeometry(0, 0, max(self.width(), 320), max(self.height(), 240))
        self._ov_sk.setVisible(kind == "busy")
        self._ov_tx.setText("正在读取大脑数据…" if kind == "busy"
                            else f"统计加载失败：{text[:120]}（下次切页或刷新会自动重试）")
        self._ov_tx.setVisible(kind != "busy")
        box.show()
        box.raise_()

    def _hide_overlay(self) -> None:
        box = getattr(self, "_ov", None)
        if box is not None:
            box.hide()

    def resizeEvent(self, e):  # noqa: N802  Qt 同名 API
        super().resizeEvent(e)
        box = getattr(self, "_ov", None)
        if box is not None and box.isVisible():
            box.setGeometry(0, 0, max(self.width(), 320), max(self.height(), 240))


def _read_health(root, stats_cache=None):
    if not root:
        return None
    try:
        return brain.health_report(root, stats_cache=stats_cache)
    except Exception:  # noqa: BLE001 体检读不出来时页面其余部分照常
        return None


def _read_distill(root):
    if not root:
        return 0
    try:
        return len(brain.distill_candidates(root, 5))
    except Exception:  # noqa: BLE001
        return 0


def _read_tasks(root):
    if not root:
        return None
    try:
        return brain.task_metrics(root, days=7)
    except Exception:  # noqa: BLE001
        return None

