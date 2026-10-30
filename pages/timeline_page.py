# -*- coding: utf-8 -*-
"""TimelinePage（P2-1 自 ui.py 拆出；P2-3a 增虚拟滚动）。

数据量策略（双路径）：
- ≤ VIRTUAL_THRESHOLD 行：旧路径——全量入内存 + 分批渲染 80 条/批（小库零感知）；
- > 阈值：虚拟滚动——数据源改 brain.records_page（SQL LIMIT/OFFSET 分页切片），
  每页在 FnWorker（QThread）后台拉取，主线程只负责把该页装进界面（Qt 控件只能在主线程建）；
  滚动条近底自动拉下一页（保留「继续加载」按钮作显式兜底）。
- 过滤器（agent/项目）在虚拟模式下下推到 SQL，翻页排序口径与 list_records 一致；
  换页只追加、不整页重建（_cur_card_lay 跨页保存，日期分组跨页连续）。
"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）

VIRTUAL_THRESHOLD = 200   # 超此行数启用虚拟滚动
PAGE_SIZE = 100           # 虚拟滚动每页条数
AUTOLOAD_MARGIN = 320     # 滚动条距底该像素内自动加载下一页
RENDER_CHUNK = 25         # 分片装入：每片行数（片间让出事件循环，防单帧长阻塞）


class TimelinePage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.all_entries: list = []
        self.projects: list = []
        self.render_limit = 80  # 懒加载：每批渲染条数（旧路径）
        self._workers: list = []       # 在飞的页面拉取线程（窗口退出前需等待）
        self._virtual = False
        self._vroot = ""
        self._vepoch = 0
        self._vstate = None
        lay = QVBoxLayout(self)
        set_page_margins(lay, bottom=0)
        lay.addWidget(_page_head("时间线", "谁在什么时候干了什么——所有 agent 的最近动态"))
        # R789：原先「筛选」标题独占一行、控件在下一行被推到最右，两者既不对齐
        # 又白吃一行高度。标题与过滤控件合成一行：左侧标题、右侧控件。
        top = QHBoxLayout()
        top.addWidget(section_head("筛选", "MENU"))
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
        lay.addSpacing(tokens.SPACE_M)   # 筛选行与动态列表是两个分区，留一档呼吸
        tip = CaptionLabel("按时间倒序 · 点击条目查看完整操作步骤 · 点击项目名跳转")
        lay.addWidget(section_head("全部动态", "HISTORY"))
        lay.addWidget(tip)
        self.scroll = ScrollArea()
        self.container = QWidget()
        self.box = QVBoxLayout(self.container)
        self.box.setContentsMargins(4, 12, 16, 24)
        self.box.setSpacing(tokens.SPACE_L)  # 日期组间距 > 组内行距 8，拉开分组
        self.scroll.setWidget(self.container)
        self.scroll.enableTransparentBackground()  # 必须在 setWidget 后（qfw API 需 widget 已存在才命中 body；2026-10-10 深底错配根因）
        self.scroll.setWidgetResizable(True)
        self.scroll.enableTransparentBackground()
        lay.addWidget(self.scroll, 1)
        self.agentFilter.currentIndexChanged.connect(self.rebuild)
        self.projFilter.currentIndexChanged.connect(self.rebuild)
        # P2-3a 虚拟滚动：滚动条近底自动拉下一页
        self.scroll.verticalScrollBar().valueChanged.connect(self._maybe_autoload)

    # ---------------------------------------------------------------- 数据入口

    def set_records(self, rows: list):
        """旧路径数据源（≤阈值）：全量入内存（dict 行）。"""
        self._virtual = False
        self._vstate = None
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

    def set_virtual(self, root: str, total: int = 0):
        """虚拟滚动数据源（>阈值）：不在内存持有全量，按页从 DB 拉取。
        窗口每次 refresh 调用；重置到第 0 页并重建过滤器下拉。"""
        self._virtual = True
        self._vroot = root
        self._vepoch += 1
        self.all_entries = []
        self.projects = []
        self._fill_filters_virtual()
        self.rebuild()

    def _fill_filters_virtual(self):
        try:
            agents = ["全部"] + [uikit.AGENT_CN.get(a, a) for a in brain.records_agents(self._vroot)]
        except Exception:
            agents = ["全部"]
        try:
            projs = ["全部"] + [p["name"] for p in brain.list_projects(self._vroot, 200)]
        except Exception:
            projs = ["全部"]
        for combo, items in ((self.agentFilter, agents), (self.projFilter, projs)):
            combo.blockSignals(True)
            cur = combo.currentText()
            combo.clear()
            combo.addItems(items)
            idx = combo.findText(cur)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    def _filtered(self):
        """旧路径内存过滤（虚拟模式不过这里——过滤下推 SQL）。"""
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

    # ---------------------------------------------------------------- 渲染

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

    def _add_content(self, w, align=None):
        """内容插在末尾吸收项之前（R789）：记录不足一屏时，富余高度留在列表底部，
        不再被 QVBoxLayout 摊成分组之间上百像素的空白。"""
        idx = max(0, self.box.count() - 1)
        if align is None:
            self.box.insertWidget(idx, w)
        else:
            self.box.insertWidget(idx, w, 0, align)

    def _clear_tail(self):
        for tw in getattr(self, "_tail", []):
            tw.setParent(None)
            tw.deleteLater()
        self._tail = []

    def _add_row(self, r):
        row = QHBoxLayout()
        row.addWidget(badge(r.get("agent", ""), ""))
        title = flex_label(ClickBodyLabel(_display_title(r)))
        title.setToolTip(f"{r.get('project', '')} · {_display_title(r)}\n点击查看完整记录")
        title.setCursor(Qt.PointingHandCursor)
        title.clicked.connect(lambda _, rr=r: self.show_detail(rr))
        # 项目名按钮：按像素自适应省略（原先按字符截 10 字+上限 140px，常见 20~27 字项目名
        # 被砍成「AgentHub-多…」，2026-10-11 修复）——宽度 240px 内完整显示，超出右端省略
        full = r.get("project", "") or ""
        proj_btn = PushButton()
        proj_btn.setText(proj_btn.fontMetrics().elidedText(full, Qt.ElideRight, 240) if full else "")
        proj_btn.setFixedHeight(26)
        proj_btn.setMaximumWidth(280)  # 上限含按钮左右内边距，防长名撑破行
        proj_btn.setToolTip(full)
        proj_btn.clicked.connect(lambda _, n=full: self.jump(n))
        row.addWidget(title, 1)
        row.addWidget(proj_btn)
        self._cur_card_lay.addLayout(row)

    def _append_rows(self, batch):
        """把一批行装进界面（日期分组；_cur_card_lay 跨批次/跨页保存——
        批次首条与上批末条同日时继续入同卡）。
        追加期间禁重绘（updatesEnabled）：一次刷成，防逐 widget 重绘抖动。"""
        self.container.setUpdatesEnabled(False)
        try:
            for r in batch:
                d = r.get("date") or "日期未标注"
                if d != self._last_date or self._cur_card_lay is None:
                    self._last_date = d
                    # 日期组不包卡：无框容器，分组靠日期标题+组间距（Linear：行是线不是卡）
                    group = QWidget()
                    # R789：记录不足一屏时，QVBoxLayout 把富余高度平摊给每个组，
                    # 日期标题与首行被拉开 70~100px（截图像素级实测）。组按内容高度
                    # 收紧，富余空间留在列表底部，不再拆散「日期 → 记录」的视觉配对。
                    group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
                    self._cur_card_lay = QVBoxLayout(group)
                    self._cur_card_lay.setContentsMargins(0, 0, 0, 0)
                    self._cur_card_lay.setSpacing(tokens.SPACE_S)
                    # 日期组标题走 section_head（2026-10-15 第二轮）：时间线页此前
                    # 整页零图标，纯 16px 灰字标题在长列表里没有视觉着陆点
                    self._cur_card_lay.addWidget(section_head(d, "CALENDAR"))
                    self._add_content(group)
                self._add_row(r)
        finally:
            self.container.setUpdatesEnabled(True)

    def _filters_active(self) -> bool:
        a = self.agentFilter.currentText()
        pj = self.projFilter.currentText()
        return bool((a and a != "全部") or (pj and pj != "全部"))

    def _empty_hint(self):
        """首次空=教，筛选空=恢复路径。原实现两处都写「放宽筛选」——
        空库（全部/全部、0 条）说自己「没有符合条件」会让新用户去翻不存在的筛选。"""
        if self.all_entries or self._filters_active():
            return empty_state("SEARCH", "没有符合条件的记录",
                               "试试放宽筛选：切换 agent / 项目下拉，或清空过滤条件")
        return empty_state("HISTORY", "还没有工作记录",
                           "agent 记一笔工作后，这里按时间倒序出现；"
                           "也可以到「接入」页让 agent 自动记录",
                           action_text="去接入页",
                           action=lambda: self.win.switchTo(self.win.connect_page))

    def rebuild(self):
        self._clear_box()
        self.box.addStretch(1)   # 末尾吸收项：内容不足一屏时富余高度留在底部
        self._clear_tail()
        self.rendered = 0
        self._last_date = None
        self._cur_card_lay = None
        if self._virtual:
            self._vepoch += 1
            self._vstate = {"offset": 0, "total": 0, "loading": False}
            self._vfetch()
        else:
            self._append_batch()

    # ---------------------------------------------------------------- 旧路径（≤阈值）

    def _append_batch(self):
        """增量渲染下一批（load_more 不整页重建：不闪屏、不丢滚动位置）。"""
        entries = self._filtered()
        batch = entries[self.rendered:self.rendered + self.render_limit]
        self._append_rows(batch)
        self.rendered += len(batch)
        if not entries:
            self._add_content(self._empty_hint())
            return
        # 尾部：加载更多按钮 + 统计说明（直接放 box，居中对齐，增量时先移除旧尾部）
        self._clear_tail()
        if self.rendered < len(entries):
            more = PushButton(f"加载更多（还有 {len(entries) - self.rendered} 条）")
            more.clicked.connect(self.load_more)
            self._add_content(more, Qt.AlignHCenter)
            self._tail.append(more)
            cap = CaptionLabel(f"共 {len(entries)} 条，已显示 {self.rendered} 条；用上方过滤器可缩小范围")
            self._add_content(cap, Qt.AlignHCenter)
            self._tail.append(cap)

    def load_more(self):
        self._append_batch()

    # ---------------------------------------------------------------- 虚拟滚动（>阈值，P2-3a）

    def _vfilters(self):
        a = self.agentFilter.currentText()
        pj = self.projFilter.currentText()
        agent = ""
        if a and a != "全部":
            agent = uikit._AGENT_DISP2ID.get(a.lower(), a).lower()
        project = "" if (not pj or pj == "全部") else pj
        return project, agent

    def _vfetch(self):
        """后台拉下一页。epoch 快照防串页：过滤器变更/重建后到达的旧页直接丢弃。"""
        st = self._vstate
        if st is None or st["loading"]:
            return
        if st["offset"] and st["offset"] >= st["total"]:
            return
        st["loading"] = True
        epoch = self._vepoch
        offset = st["offset"]
        project, agent = self._vfilters()
        root = self._vroot

        def _job():
            return brain.records_page(root, offset=offset, limit=PAGE_SIZE,
                                      project=project, agent=agent)

        w = FnWorker(_job, self)
        w.done.connect(lambda res, e=epoch: self._von_page(res, e))
        w.finished.connect(lambda wk=w: self._workers.remove(wk) if wk in self._workers else None)
        self._workers.append(w)
        w.start()

    def _von_page(self, res, epoch):
        if epoch != self._vepoch:
            return  # 过期页（过滤器变更/重建后到达）→ 丢弃
        st = self._vstate
        if st is None:
            return
        if isinstance(res, Exception):
            st["loading"] = False
            self._add_content(empty_state("INFO", "加载失败", str(res)[:120]))
            return
        rows, total = res
        st["total"] = total
        if not rows:
            st["loading"] = False
            if st["offset"] > 0:
                st["offset"] = st["total"]  # 防御：翻到空页直接视为到底，防自动加载死循环
            else:
                self._add_content(self._empty_hint())
            self._vtail()
            return
        # 分片装入：loading 保持 True 直到装完（期间不触发下一页拉取，防级联）
        self._drain_rows(list(rows), len(rows), st, epoch)

    def _drain_rows(self, pending, total_rows, st, epoch):
        """分片渲染：每片 RENDER_CHUNK 行，片间 QTimer(0) 让出事件循环。
        大页（100 行）单次装入的布局/重绘可达秒级（5000 条压测实测 ~1-2s 峰值帧），
        分片后单帧工作量恒定，滚动与交互不被长帧打断。"""
        if epoch != self._vepoch or self._vstate is not st:
            return  # 过滤器变更/重建 → 放弃剩余分片
        chunk, rest = pending[:RENDER_CHUNK], pending[RENDER_CHUNK:]
        self._append_rows(chunk)
        if rest:
            defer(self, lambda: self._drain_rows(rest, total_rows, st, epoch))
        else:
            st["offset"] += total_rows
            st["loading"] = False
            self._vtail()
            self._maybe_autoload()  # 若用户仍停在底部，继续供页（无限滚动延续）

    def _vtail(self):
        self._clear_tail()
        st = self._vstate
        if not st:
            return
        if st["offset"] < st["total"]:
            more = PushButton(f"继续加载（已显示 {st['offset']} / {st['total']} 条）")
            more.clicked.connect(self._vfetch)
            self._add_content(more, Qt.AlignHCenter)
            self._tail.append(more)
            cap = CaptionLabel(f"共 {st['total']} 条 · 滚动到底自动加载更多")
            self._add_content(cap, Qt.AlignHCenter)
            self._tail.append(cap)
        else:
            cap = CaptionLabel(f"共 {st['offset']} 条，已全部加载")
            self._add_content(cap, Qt.AlignHCenter)
            self._tail.append(cap)

    def _maybe_autoload(self, _value=None):
        """滚动条近底 → 自动拉下一页（无限滚动）。页高达不到一屏时不会级联，
        因为每页 100 行远超视口高度，追加后底部远离当前视点。"""
        if not self._virtual or not self._vstate:
            return
        bar = self.scroll.verticalScrollBar()
        if bar.value() >= bar.maximum() - AUTOLOAD_MARGIN:
            self._vfetch()

    # ---------------------------------------------------------------- 交互

    def show_detail(self, r):
        dlg = RecordDetailDialog(self.win, r)
        dlg.exec()

    def jump(self, name):
        self.win.project_page.select_project(name)
        self.win.switchTo(self.win.project_page)
