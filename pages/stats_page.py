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
        row = QHBoxLayout()
        self.valProj, self.valRec, self.valIssue, self.valStalled, self.valSearch = \
            (TitleLabel("0") for _ in range(5))
        for val, t in ((self.valProj, "项目总数"), (self.valRec, "工作记录条数"),
                       (self.valIssue, "对账问题数"), (self.valStalled, "停滞项目（90天）"),
                       (self.valSearch, "检索（今日/累计）")):
            col = QVBoxLayout()
            col.addWidget(val)
            col.addWidget(CaptionLabel(t))
            row.addLayout(col)
        lay.addLayout(row)

        self.agentBox = QVBoxLayout()
        self.monthBox = QVBoxLayout()
        self.healthLine = CaptionLabel("")
        self.healthLine.setWordWrap(True)
        # 内容组不包卡：间距+小标题分组
        for box, t in ((self.agentBox, "各 agent 记录分布"), (self.monthBox, "近 6 个月活跃")):
            lay.addSpacing(16)
            lay.addWidget(StrongBodyLabel(t))
            lay.addLayout(box)
        lay.addSpacing(16)
        lay.addWidget(StrongBodyLabel("大脑体检（智能摘要）"))
        lay.addWidget(self.healthLine)
        lay.addStretch(1)

    def set_db(self, s: dict):
        """统计页数据源 = 大脑数据库。"""
        self.valProj.setText(str(s.get("projects", 0)))
        self.valRec.setText(str(s.get("records", 0)))
        self.valIssue.setText(str(s.get("errors_open", 0)))
        self.valStalled.setText(str(s.get("projects_stalled", 0)))
        self.valSearch.setText(f"{s.get('searches_today', 0)} / {s.get('searches_total', 0)}")
        try:
            h = brain.health_report(self.win.root)
            dup = f"疑似重复记忆 {h['dup_memory_count']} 组"
            todo = f"待办线索 {h['todo_count']} 条"
            tip = []
            if h['dup_memory_count']:
                tip.append(dup)
            if h['todo_count']:
                tip.append(todo)
            if h['projects_stalled']:
                tip.append(f"停滞项目 {h['projects_stalled']} 个")
            try:
                dc = len(brain.distill_candidates(self.win.root, 5))
            except Exception:
                dc = 0
            if dc:
                tip.append(f"蒸馏候选 {dc} 条")
            self.healthLine.setText(
                f"结晶率 {h['crystallization']}%（记忆/记录）· 检索 今日 {h['searches_today']} / 累计 {h['searches_total']} 次"
                + (" · " + " · ".join(tip) if tip else " · 各项整洁")
                + "（agent 可调 hub_health / hub_distill 看完整报告）")
        except Exception:
            self.healthLine.setText("体检暂不可用")

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

