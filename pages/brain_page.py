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
        lay.setContentsMargins(24, 24, 24, 24)
        lay.addWidget(_page_head("大脑", "公用记忆的运转状态——唤起排行 / 使用分布 / 待办看板 / 置顶记忆"))
        # 健康读数：纯展示不包卡（卡片语义留给可点区域），大数字+小标题直接排版
        row = QHBoxLayout()
        self.valMem, self.valCrys, self.valPush, self.valTodo = (TitleLabel("0") for _ in range(4))
        for val, t in ((self.valMem, "记忆总数"), (self.valCrys, "结晶率（记忆/记录）"),
                       (self.valPush, "记忆推送累计"), (self.valTodo, "待办线索")):
            col = QVBoxLayout()
            col.addWidget(val)
            col.addWidget(CaptionLabel(t))
            row.addLayout(col)
        lay.addLayout(row)

        # 随记已独立成页（NotesPage——人的模块容器），大脑页回归 agent 数据

        self.topBox = QVBoxLayout()
        self.useBox = QVBoxLayout()
        self.todoBox = QVBoxLayout()
        self.pinBox = QVBoxLayout()
        # 内容组不包卡：间距+小标题分组（Fluent Layout——分组靠 spacing，不需要每块加框）
        for box, t in ((self.topBox, "记忆唤起 top10（开工推送反射弧记数）"),
                       (self.useBox, "大脑使用分布（主动检索 + 推送接收，按 agent）"),
                       (self.todoBox, "待办看板（记录里提取的欠账，处理后勾销闭环）"),
                       (self.pinBox, "置顶记忆（置顶位只放工作知识）")):
            box.setSpacing(8)  # 行距：无间距时唤起/置顶行挤成一片
            lay.addSpacing(16)  # 组间距 > 行距，拉开层级
            lay.addWidget(StrongBodyLabel(t))
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
        h.setSpacing(8)
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

