# -*- coding: utf-8 -*-
"""PlaybookPage（GUI 管理面·v2.40）：编排中心——Y1/Y2/Y3 的桌面端。

hub_playbook / hub_trigger / hub_graph 的 GUI 呈现：
  流程（清单+跑一次）→ 步骤（选中流程的分步）→ 执行历史（续跑/中止）→ 触发器（启停）
外加「重建图谱边」。桌面端不必开 MCP 会话也能驱动同一套编排层。
"""
from __future__ import annotations

from PySide6.QtWidgets import QListWidgetItem, QInputDialog  # noqa: F401

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401
import brain


class PlaybookPage(QWidget):
    """编排中心：流程 / 步骤 / 执行历史 / 触发器（SegmentedWidget 切换）。"""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self._flows: dict = {}    # id(item) -> 流程行
        self._runs: dict = {}     # id(item) -> 执行行
        self._trig: dict = {}     # id(item) -> 触发器行
        self._empty_widgets: dict = {}   # 分段 -> 空态卡片（五段文案/CTA 各不同）
        self.cur_flow = ""
        self.cur_run = 0     # 执行历史里展开分步的那一条
        self.cur_trig = 0    # 触发器启停后刷新时保持选中
        self._loading = False  # 刷新期间恢复选中会再发选中信号，必须挡住重入
        lay = QVBoxLayout(self)
        self._lay = lay
        set_page_margins(lay)
        lay.addWidget(_page_head(
            "编排中心",
            "把每次都要手工重走的流程固化成可执行的流程模板，加上事件自动触发与知识图谱关联。"
            "桌面端可直接跑流程、续跑断点、启停触发器——不必开 MCP 会话"))

        top = QWidget()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        _i1 = IconWidget(ic("PEOPLE", "INFO"), top); _i1.setFixedSize(16, 16)
        tl.addWidget(_i1)
        tl.addWidget(StrongBodyLabel("身份"))
        self.agentCombo = ComboBox()
        self.agentCombo.setMinimumWidth(110)
        tl.addWidget(self.agentCombo)
        _i2 = IconWidget(ic("FOLDER", "INFO"), top); _i2.setFixedSize(16, 16)
        tl.addWidget(_i2)
        tl.addWidget(StrongBodyLabel("项目"))
        self.projEdit = LineEdit()
        # R197：{{project}} 是模板语法，用户看到的应是它解决的问题——「流程里的项目名占位」
        self.projEdit.setPlaceholderText("项目名（可选，用来把图文留痕归到这个项目下）")
        self.projEdit.setToolTip("流程模板里的 {{project}} 会替换成这里填的项目名；不填也能跑")
        tl.addWidget(self.projEdit, 1)
        lay.addWidget(top)

        self.seg = SegmentedWidget(self)
        self.seg.addItem("flows", "流程")
        self.seg.addItem("steps", "步骤")
        self.seg.addItem("runs", "执行历史")
        self.seg.addItem("trig", "触发器")
        self.seg.addItem("graph", "图谱")
        self.seg.setCurrentItem("flows")  # 默认段：流程（原两个都不选，且 reload 恒落 graph）
        self.listw = ListWidget()
        self.listw.setMinimumHeight(300)
        lay.addWidget(self.seg)
        lay.addWidget(self.listw, 1)

        row = QHBoxLayout()
        self.runBtn = PushButton(ic("PLAY", "INFO"), "跑一次")
        self.runBtn.setToolTip("跑选中的流程（会真的执行动作：检查/备份/写记录等）")
        self.runBtn.clicked.connect(self.run_flow)
        row.addWidget(self.runBtn)
        self.resumeBtn = PushButton(ic("SYNC", "INFO"), "续跑选中")
        self.resumeBtn.setToolTip("从断点继续（人工步骤视为已做完，失败步骤重试）")
        self.resumeBtn.clicked.connect(self.resume_run)
        row.addWidget(self.resumeBtn)
        self.abortBtn = PushButton("中止选中")
        self.abortBtn.clicked.connect(self.abort_run)
        row.addWidget(self.abortBtn)
        self.togBtn = PushButton("启/停触发器")
        self.togBtn.clicked.connect(self.toggle_trig)
        row.addWidget(self.togBtn)
        self.buildBtn = PushButton("重建图谱边")
        self.buildBtn.setToolTip("重扫标签/项目生成结构边（幂等，双链与手工边不动）")
        self.buildBtn.clicked.connect(self.build_graph)
        row.addWidget(self.buildBtn)
        row.addStretch(1)
        refreshBtn = PushButton(ic("SYNC", "INFO"), "刷新")
        refreshBtn.clicked.connect(self.reload)
        row.addWidget(refreshBtn)
        lay.addLayout(row)

        self.seg.currentItemChanged.connect(self._on_seg_changed)
        self.listw.itemDoubleClicked.connect(lambda _it: self.run_flow())
        self.listw.itemSelectionChanged.connect(self._on_select)
        self._sync_actions()

    def _on_seg_changed(self, _key):
        self.reload()

    def _on_select(self):
        """记下选中项：流程给「步骤」段看，执行/触发器给续跑与启停用。"""
        if self._loading:
            return
        seg = self.seg.currentRouteKey()
        row = self._selected()
        if seg == "flows" and row:
            self.cur_flow = str(row.get("name") or "")
        elif seg == "runs" and row:
            rid = int(row.get("id") or 0)
            if rid and rid != self.cur_run:
                self.cur_run = rid
                self.reload()  # 选中哪条就展开哪条的分步
                return
        elif seg == "trig" and row:
            self.cur_trig = int(row.get("id") or 0)
        self._sync_actions()

    def _sync_actions(self):
        """按钮跟着分段/选中走：没有可作用的对象就置灰，别让用户点了才吃警告。"""
        seg = self.seg.currentRouteKey()
        row = self._selected()
        ok = bool(self.win.root)
        run_open = (seg == "runs" and row is not None
                    and str(row.get("status") or "") not in ("done", "aborted"))
        self.runBtn.setEnabled(ok and seg == "flows" and row is not None)
        self.resumeBtn.setEnabled(ok and run_open)
        self.abortBtn.setEnabled(ok and run_open)
        self.togBtn.setEnabled(ok and seg == "trig" and row is not None)
        self.buildBtn.setEnabled(ok and seg == "graph")

    def _empty_pane(self, key, icon, title, desc="", action_text="", action=None):
        """按分段懒建空态卡片。

        不用 list_empty_guard 是因为它一个列表只挂一个实例、文案建后不可换；
        这里五段各需要不同的说明与 CTA。内容变了（如读取失败的报错）才重建。
        """
        sig = (icon, title, desc, action_text)
        box = self._empty_widgets.get(key)
        if box is not None and box.property("emptySig") == sig:
            return box
        if box is not None:
            box.setParent(None)
            box.deleteLater()
        box = empty_state(icon, title, desc, action_text=action_text, action=action)
        box.setProperty("emptySig", sig)
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        inner = box.layout()
        if inner is not None:
            # 列表已藏起来，这块就是页面主区域：垂直居中才像引导，不像贴在顶部的报错条
            inner.insertStretch(0, 1)
            inner.addStretch(1)
        self._empty_widgets[key] = box
        self._lay.insertWidget(self._lay.indexOf(self.listw) + 1, box)
        return box

    def _show_empty(self, key, icon, title, desc="", action_text="", action=None):
        self._empty_pane(key, icon, title, desc, action_text, action)
        for k, box in self._empty_widgets.items():
            box.setVisible(k == key)
        self.listw.setVisible(False)

    def _show_list(self):
        for box in self._empty_widgets.values():
            box.setVisible(False)
        self.listw.setVisible(True)

    def _load_error(self, key, err):
        self._show_empty(f"{key}-error", "INFO", "读取失败",
                         f"{err}（点「刷新」重试；若持续失败，到「接入」页确认数据目录可用）",
                         "刷新", self.reload)

    # ---------- 数据 ----------
    def _sync_agents(self):
        keep = self.agentCombo.currentText()
        try:
            names = [a["name"] for a in brain.list_agents(self.win.root)]
        except Exception:  # noqa: BLE001
            names = []
        for extra in ("workbuddy", "zcode", "dsh", "claude", "codex", "hermes"):
            if extra not in names:
                names.append(extra)
        self.agentCombo.clear()
        self.agentCombo.addItems(names)
        if keep in names:
            self.agentCombo.setCurrentText(keep)

    def reload(self):
        self._loading = True
        try:
            self.listw.clear()
            self._flows.clear()
            self._runs.clear()
            self._trig.clear()
            if not self.win.root:
                self._show_empty(
                    "noroot", "LINK", "还没有连接大脑",
                    "先到「接入」页选择数据目录（通常是 D:\\agent资料\\zcode项目记录），"
                    "再回来跑流程、看执行历史与知识图谱。",
                    "去接入页", lambda: self.win.switchTo(self.win.connect_page))
                return
            self._sync_agents()

            seg = self.seg.currentRouteKey()
            if seg == "flows":
                self._load_flows()
            elif seg == "steps":
                self._load_steps()
            elif seg == "runs":
                self._load_runs()
            elif seg == "trig":
                self._load_trig()
            else:
                self._load_graph()
        finally:
            self._loading = False
            self._sync_actions()

    def _add(self, text, store=None, row=None):
        it = QListWidgetItem(text)
        if store is not None and row is not None:
            store[id(it)] = row
        self.listw.addItem(it)
        return it

    def _load_flows(self):
        try:
            rows = brain.playbook_list(self.win.root)
        except Exception as e:  # noqa: BLE001
            self._load_error("flows", e)
            return
        if not rows:
            self._show_empty(
                "flows-empty", "PLAY", "还没有流程模板",
                "内置流程通常随首次启动创建。可点「刷新」重试；若仍为空，"
                "检查大脑数据目录是否完整，或用 hub_playbook action=create 新建。",
                "刷新", self.reload)
            return
        self._show_list()
        keep = None
        for p in rows:
            tag = "内置" if int(p.get("builtin") or 0) else "自定义"
            it = self._add(f"[{tag}] {p['name']} — {p['title']}（{p['n_steps']} 步 · "
                           f"成功跑过 {p['n_done']} 次 · 最近：{p.get('last_status') or '未跑过'}）",
                           self._flows, p)
            if self.cur_flow == p["name"]:
                keep = it
        if keep is not None:
            self.listw.setCurrentItem(keep)  # 跑完/切段回来仍停在同一条，按钮不至于莫名置灰
        elif self.listw.count():
            # R198：首次进来一条都没选中，「跑一次」等按钮全灰，五个动作像是不可用。
            # 有数据就默认选中第一条（与搜索/时间线「命中即选中」一致），语义上它是
            # 「最近/首条内置流程」，选中它不会执行任何动作。
            self.listw.setCurrentRow(0)
            row = self._selected()
            if row:
                self.cur_flow = str(row.get("name") or "")
        elif self.cur_flow:
            self.cur_flow = ""

    def _load_steps(self):
        if not self.cur_flow:
            self._show_empty(
                "steps-noflow", "PLAY", "先在「流程」段选中一条流程",
                "选中后回到这里，就能看到它的每一步动作、参数与备注，跑之前先心里有数。",
                "去流程段", lambda: self.seg.setCurrentItem("flows"))
            return
        try:
            pb = brain.playbook_get(self.win.root, self.cur_flow)
        except Exception as e:  # noqa: BLE001
            self._load_error("steps", e)
            return
        if not pb:
            name = self.cur_flow
            self.cur_flow = ""
            self._show_empty(
                "steps-missing", "INFO", f"流程「{name}」不存在",
                "它可能已被删除或改名。回「流程」段重新选一条即可。",
                "去流程段", lambda: self.seg.setCurrentItem("flows"))
            return
        self._show_list()
        m = pb["meta"]
        head = QListWidgetItem(f"── {m['name']} — {m['title']}"
                               f"（{'内置模板' if m.get('builtin') else '自定义'}）")
        head.setFlags(Qt.ItemFlag.NoItemFlags)
        self.listw.addItem(head)
        if m.get("desc"):
            d = QListWidgetItem(f"   {m['desc']}")
            d.setFlags(Qt.ItemFlag.NoItemFlags)
            self.listw.addItem(d)
        for s in pb["steps"]:
            args = s.get("args") or {}
            a = " ".join(f"{k}={v}" for k, v in args.items())
            note = f"｜{s['note']}" if s.get("note") else ""
            self.listw.addItem(f"  {s['seq']}. {s['title']}  [{s['action']}]  {a}{note}")

    def _load_runs(self):
        try:
            rows = brain.playbook_runs(self.win.root, limit=20)
        except Exception as e:  # noqa: BLE001
            self._load_error("runs", e)
            return
        if not rows:
            self._show_empty(
                "runs-empty", "HISTORY", "还没有执行记录",
                "在「流程」段选中一条流程点「跑一次」，每次执行都会留档；"
                "失败或停在人工步骤时，可以在这里续跑或中止。",
                "去跑一次", lambda: self.seg.setCurrentItem("flows"))
            return
        self._show_list()
        keep = None
        for r in rows:
            it = self._add(f"#{r['id']} {r['playbook']} [{r['status']}] {r['summary']}"
                           f"（{r['project'] or '无项目'} · {r['agent'] or '无'} · {rel_time(r['started'])}）",
                           self._runs, r)
            if r["id"] == self.cur_run:
                keep = it
            # 选中的那条就地展开分步：失败停在第几步、每步输出是什么，桌面端也得看得见
            if r["id"] == self.cur_run:
                try:
                    steps = brain.playbook_run_steps(self.win.root, r["id"])
                except Exception:  # noqa: BLE001
                    steps = []
                for s in steps:
                    mark = {"done": "√", "failed": "×", "manual": "手",
                            "pending": "·", "skipped": "-"}.get(s["status"], "·")
                    out = f"  → {(s.get('output') or '')[:70]}" if s.get("output") else ""
                    it = QListWidgetItem(f"    [{mark}] {s['seq']}. {s['title']}{out}")
                    it.setFlags(Qt.ItemFlag.NoItemFlags)  # 分步不是可选项，避免误选
                    self.listw.addItem(it)
        if keep is not None:
            self.listw.setCurrentItem(keep)  # 展开分步后仍选中该条，「续跑/中止」立即可用
        elif self.cur_run:
            self.cur_run = 0

    def _load_trig(self):
        try:
            rows = brain.trigger_list(self.win.root)
        except Exception as e:  # noqa: BLE001
            self._load_error("trig", e)
            return
        if not rows:
            self._show_empty(
                "trig-empty", "SYNC", "还没有触发器",
                "触发器让流程按事件自动运行（例如任务完成后自动记一笔）。"
                "用 hub_trigger action=add 创建，默认停用——确认规则后再启用。",
                "刷新", self.reload)
            return
        self._show_list()
        keep = None
        for t in rows:
            state = "启用" if int(t.get("enabled") or 0) else "停用"
            it = self._add(f"#{t['id']} [{state}] {t['name']}：{t['on_event']} → {t['action_type']}"
                           f"（冷却 {t['cooldown_min']} 分钟 · 已触发 {t['fire_count']} 次 · "
                           f"上次 {rel_time(t.get('last_fired')) if t.get('last_fired') else '从未'}）",
                           self._trig, t)
            if int(t["id"]) == self.cur_trig:
                keep = it
        if keep is not None:
            self.listw.setCurrentItem(keep)  # 启停后刷新仍选中原触发器，方便连续操作
        elif self.cur_trig:
            self.cur_trig = 0

    def _load_graph(self):
        """图谱段：有内容时展示簇/孤岛清单；空图谱给一条可执行的恢复路径。"""
        try:
            st = brain.graph_stats(self.win.root, build=False)
        except Exception as e:  # noqa: BLE001
            self._load_error("graph", e)
            return
        try:
            cl = brain.graph_clusters(self.win.root, limit=5, build=False)
        except Exception:  # noqa: BLE001
            cl = []
        try:
            iso = brain.graph_isolated(self.win.root, limit=15)
        except Exception:  # noqa: BLE001
            iso = []
        if not cl and not iso:
            bk = "、".join(f"{k} {v}" for k, v in (st.get("by_kind") or {}).items()) or "无"
            self._show_empty(
                "graph-empty", "CONNECT", "图谱还没有成规模的簇",
                f"当前 {st.get('edges', 0)} 条结构边（{bk}）· 已连 {st.get('linked', 0)} 条 · "
                f"孤岛 {st.get('isolated', 0)} 条。给记录补标签或 [[双链]] 后重建，"
                "检索更容易唤起它们。",
                "重建图谱边", self.build_graph)
            return
        self._show_list()
        bk = "、".join(f"{k} {v}" for k, v in (st.get("by_kind") or {}).items()) or "无"
        head = QListWidgetItem(
            f"── 边 {st.get('edges', 0)} 条（{bk}）｜已连条目 {st.get('linked', 0)}"
            f" · 孤岛 {st.get('isolated', 0)} · 最大簇 {st.get('largest_cluster', 0)}")
        head.setFlags(Qt.ItemFlag.NoItemFlags)
        self.listw.addItem(head)
        if cl:
            c = QListWidgetItem("知识簇（规模降序）：")
            c.setFlags(Qt.ItemFlag.NoItemFlags)
            self.listw.addItem(c)
            for g in cl:
                self.listw.addItem(f"  {g['size']} 个条目：" + "；".join((g.get("sample") or [])[:2]))
        if iso:
            c = QListWidgetItem(f"孤岛 {len(iso)} 条（无边相连，检索最难唤起它们）：")
            c.setFlags(Qt.ItemFlag.NoItemFlags)
            self.listw.addItem(c)
            for r in iso:
                proj = f"[{r['project']}] " if r.get("project") else ""
                self.listw.addItem(f"  {r['node']} {proj}{r['snippet']}")
            tip = QListWidgetItem("  处理：补标签（记忆）或建 [[双链]]（记录），再点「重建图谱边」")
            tip.setFlags(Qt.ItemFlag.NoItemFlags)
            self.listw.addItem(tip)

    def _selected(self):
        it = self.listw.currentItem()
        if it is None:
            return None
        seg = self.seg.currentRouteKey()
        store = {"flows": self._flows, "runs": self._runs, "trig": self._trig}.get(seg)
        return store.get(id(it)) if store else None

    # ---------- 操作 ----------
    def run_flow(self):
        if not self.win.root:
            return
        row = self._selected()
        if self.seg.currentRouteKey() != "flows" or not row:
            InfoBar.warning("先选中一条流程", "切到「流程」段并选中要跑的流程", parent=self)
            return
        name = row["name"]
        proj = self.projEdit.text().strip()
        if not proj:
            proj, ok = QInputDialog.getText(self, "跑流程", "项目名（可选，会替换流程里的占位）：")
            if not ok:
                return
            proj = (proj or "").strip()
        agent = self.agentCombo.currentText()
        try:
            res = brain.playbook_run(self.win.root, name, proj, agent)
        except Exception as e:  # noqa: BLE001
            InfoBar.error("执行失败", str(e), parent=self)
            return
        if res.get("error"):
            InfoBar.error("执行失败", str(res["error"]), parent=self)
            return
        ok = res["status"] in ("done", "waiting")
        (InfoBar.success if ok else InfoBar.warning)(
            f"playbook {name}", f"run #{res['run_id']}：{res['summary']}", duration=5000, parent=self)
        self.cur_flow = name
        self.reload()

    def resume_run(self):
        if not self.win.root:
            return
        row = self._selected()
        if self.seg.currentRouteKey() != "runs" or not row:
            InfoBar.warning("先选中一条执行记录", "切到「执行历史」段并选中一条", parent=self)
            return
        try:
            res = brain.playbook_resume(self.win.root, row["id"], self.agentCombo.currentText())
        except Exception as e:  # noqa: BLE001
            InfoBar.error("续跑失败", str(e), parent=self)
            return
        if res.get("error"):
            InfoBar.warning("无法续跑", str(res["error"]), parent=self)
            return
        (InfoBar.success if res["status"] == "done" else InfoBar.warning)(
            f"run #{row['id']}", res["summary"], duration=5000, parent=self)
        self.reload()

    def abort_run(self):
        row = self._selected()
        if self.seg.currentRouteKey() != "runs" or not row:
            InfoBar.warning("先选中一条执行记录", "切到「执行历史」段并选中一条", parent=self)
            return
        try:
            msg = brain.playbook_abort(self.win.root, row["id"])
        except Exception as e:  # noqa: BLE001
            InfoBar.error("中止失败", str(e), parent=self)
            return
        InfoBar.info("已处理", msg, parent=self)
        self.reload()

    def toggle_trig(self):
        row = self._selected()
        if self.seg.currentRouteKey() != "trig" or not row:
            InfoBar.warning("先选中一条触发器", "切到「触发器」段并选中一条", parent=self)
            return
        new_state = not int(row.get("enabled") or 0)
        try:
            msg = brain.trigger_set_enabled(self.win.root, row["id"], new_state)
        except Exception as e:  # noqa: BLE001
            op_failed("切换触发器", e, parent=self)
            return
        # Round 1：原标题只有名词「触发器」，缺结果——用户看不出是启用还是停用
        InfoBar.success("触发器已启用" if new_state else "触发器已停用", msg, parent=self)
        self.reload()

    def build_graph(self):
        if not self.win.root:
            return
        try:
            s = brain.graph_build(self.win.root)
        except Exception as e:  # noqa: BLE001
            InfoBar.error("重建失败", str(e), parent=self)
            return
        InfoBar.success("图谱结构边已重建",
                        f"标签边 {s['tag']} · 项目边 {s['in']}", duration=4000, parent=self)
        self.reload()  # 重建后立刻反映新的边数/簇规模；空态也随之收起或更新
