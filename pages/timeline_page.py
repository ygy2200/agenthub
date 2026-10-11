# -*- coding: utf-8 -*-
"""TimelinePage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
class TimelinePage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.all_entries: list = []
        self.projects: list = []
        self.render_limit = 80  # 懒加载：每批渲染条数
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 0)
        lay.addWidget(_page_head("时间线", "谁在什么时候干了什么——所有 agent 的最近动态"))
        top = QHBoxLayout()
        top.addStretch(1)
        top.addWidget(CaptionLabel("agent"))
        self.agentFilter = ComboBox()
        self.agentFilter.setFixedWidth(130)
        top.addWidget(self.agentFilter)
        top.addWidget(CaptionLabel("项目"))
        self.projFilter = ComboBox()
        self.projFilter.setFixedWidth(220)
        top.addWidget(self.projFilter)
        lay.addLayout(top)
        tip = CaptionLabel("按时间倒序 · 点击条目查看完整操作步骤 · 点击项目名跳转")
        lay.addWidget(tip)
        self.scroll = ScrollArea()
        self.container = QWidget()
        self.box = QVBoxLayout(self.container)
        self.box.setContentsMargins(4, 12, 16, 24)
        self.box.setSpacing(16)  # 日期组间距 > 组内行距 8，拉开分组
        self.scroll.setWidget(self.container)
        self.scroll.enableTransparentBackground()  # 必须在 setWidget 后（qfw API 需 widget 已存在才命中 body；2026-10-10 深底错配根因）
        self.scroll.setWidgetResizable(True)
        self.scroll.enableTransparentBackground()
        lay.addWidget(self.scroll, 1)
        self.agentFilter.currentIndexChanged.connect(self.rebuild)
        self.projFilter.currentIndexChanged.connect(self.rebuild)

    def set_records(self, rows: list):
        """时间线数据源 = 大脑数据库 records 表（dict 行）。"""
        self.projects = sorted({r["project"] for r in rows})
        entries = rows
        self.all_entries = entries

        # 过滤器填充（保持当前选择）；agent 候选统一小写去重（大小写双身份曾致过滤滤光记录）；
        # 项目列表取全量（无记录项目也可过滤）
        agents = ["全部"] + [uikit.AGENT_CN.get(a, a) for a in
                             sorted({(r.get("agent") or "").lower() for r in rows} - {""})]
        try:
            all_projects = [p["name"] for p in brain.list_projects(self.win.root, 200)]
        except Exception:
            all_projects = self.projects
        projs = ["全部"] + all_projects
        for combo, items in ((self.agentFilter, agents), (self.projFilter, projs)):
            combo.blockSignals(True)
            cur = combo.currentText()
            combo.clear()
            combo.addItems(items)
            idx = combo.findText(cur)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)
        self.rebuild()  # 新快照：全清重渲（rendered/_last_date 重置）

    def _filtered(self):
        a = self.agentFilter.currentText()
        pj = self.projFilter.currentText()
        out = self.all_entries
        if a and a != "全部":
            # 显示名反查库内 id 再比（2026-10-10）：下拉显示的是显示名（"Claude Code"/
            # "WorkBuddy"），而库里存的是 id（claude/workbuddy）。直接拿显示名比会滤光记录。
            want = uikit._AGENT_DISP2ID.get(a.lower(), a).lower()
            # casefold 比较：agent 存值大小写曾混杂（ZCode/zcode），精确 == 会滤光记录
            out = [r for r in out if (r.get("agent") or "").lower() == want]
        if pj and pj != "全部":
            out = [r for r in out if r.get("project", "") == pj]
        return out

    def _clear_layout(self, lay):
        """递归清理子布局：只 deleteLater widget 项会让装在子布局里的按钮泄漏残留
        （2026-10-01 时间线"加载更多"按钮堆叠 bug 根因）。"""
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                self._clear_layout(it.layout())
                it.layout().deleteLater()

    def _clear_box(self):
        while self.box.count():
            item = self.box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())
                item.layout().deleteLater()

    def rebuild(self):
        self._clear_box()
        self.rendered = 0
        self._last_date = None
        self._cur_card_lay = None
        self._append_batch()

    def _append_batch(self):
        """增量渲染下一批（load_more 不整页重建：不闪屏、不丢滚动位置）。
        _cur_card_lay 跨批次保存最后一张日期卡——批次首条与上批末条同日时继续入同卡。"""
        entries = self._filtered()
        batch = entries[self.rendered:self.rendered + self.render_limit]
        for r in batch:
            d = r.get("date") or "日期未标注"
            if d != self._last_date or self._cur_card_lay is None:
                self._last_date = d
                # 日期组不包卡：无框容器，分组靠日期标题+组间距（Linear：行是线不是卡）
                group = QWidget()
                self._cur_card_lay = QVBoxLayout(group)
                self._cur_card_lay.setContentsMargins(0, 0, 0, 0)
                self._cur_card_lay.setSpacing(8)
                self._cur_card_lay.addWidget(StrongBodyLabel(d))
                self.box.addWidget(group)
            row = QHBoxLayout()
            row.addWidget(badge(r.get("agent", ""), ""))
            title = flex_label(ClickBodyLabel(_display_title(r)[:80]))
            title.setToolTip(f"{r.get('project', '')} · {_display_title(r)}\n点击查看完整记录")
            title.setCursor(Qt.PointingHandCursor)
            title.clicked.connect(lambda _, rr=r: self.show_detail(rr))
            proj_btn = PushButton(r.get("project", "")[:10] + ("…" if len(r.get("project", "")) > 10 else ""))
            proj_btn.setFixedHeight(26)
            proj_btn.setMaximumWidth(140)  # 长项目名不设上限会把行撑出可视区，按钮被裁成半个
            proj_btn.setToolTip(r.get("project", ""))
            proj_btn.clicked.connect(lambda _, n=r.get("project", ""): self.jump(n))
            row.addWidget(title, 1)
            row.addWidget(proj_btn)
            self._cur_card_lay.addLayout(row)
        self.rendered += len(batch)
        if not entries:
            self.box.addWidget(empty_state(
                "SEARCH", "没有符合条件的记录",
                "试试放宽筛选：切换 agent / 项目下拉，或清空过滤条件"))
            return
        # 尾部：加载更多按钮 + 统计说明（直接放 box，居中对齐，增量时先移除旧尾部）
        for tw in getattr(self, "_tail", []):
            tw.setParent(None)
            tw.deleteLater()
        self._tail = []
        if self.rendered < len(entries):
            more = PushButton(f"加载更多（还有 {len(entries) - self.rendered} 条）")
            more.clicked.connect(self.load_more)
            self.box.addWidget(more, 0, Qt.AlignHCenter)
            self._tail.append(more)
            cap = CaptionLabel(f"共 {len(entries)} 条，已显示 {self.rendered} 条；用上方过滤器可缩小范围")
            self.box.addWidget(cap, 0, Qt.AlignHCenter)
            self._tail.append(cap)

    def load_more(self):
        self._append_batch()

    def show_detail(self, r):
        dlg = RecordDetailDialog(self.win, r)
        dlg.exec()

    def jump(self, name):
        self.win.project_page.select_project(name)
        self.win.switchTo(self.win.project_page)

