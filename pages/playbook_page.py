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
        self.cur_flow = ""
        self.cur_run = 0     # 执行历史里展开分步的那一条
        lay = QVBoxLayout(self)
        set_page_margins(lay)
        lay.addWidget(_page_head(
            "编排中心",
            "把每次都要手工重走的流程固化成**可执行**的 playbook（Y1）+ 事件自动触发（Y2）+ "
            "知识图谱关联（Y3）。桌面端可直接跑流程、续跑断点、启停触发器——不必开 MCP 会话"))

        top = QWidget()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(StrongBodyLabel("身份"))
        self.agentCombo = ComboBox()
        self.agentCombo.setMinimumWidth(110)
        tl.addWidget(self.agentCombo)
        tl.addWidget(StrongBodyLabel("项目"))
        self.projEdit = LineEdit()
        self.projEdit.setPlaceholderText("流程里的 {{project}}（留痕用，可空）")
        tl.addWidget(self.projEdit, 1)
        lay.addWidget(top)

        self.seg = SegmentedWidget(self)
        self.seg.addItem("flows", "流程")
        self.seg.addItem("steps", "步骤")
        self.seg.addItem("runs", "执行历史")
        self.seg.addItem("trig", "触发器")
        self.seg.addItem("graph", "图谱")
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

        self.seg.currentItemChanged.connect(lambda _k: self.reload())
        self.listw.itemDoubleClicked.connect(lambda _it: self.run_flow())
        self.listw.itemSelectionChanged.connect(self._on_select)

    def _on_select(self):
        """执行历史段：选中哪条就展开哪条的分步（变化时刷新一次，不重复递归）。"""
        if self.seg.currentItem() != "runs":
            return
        row = self._selected()
        if row and int(row.get("id") or 0) != self.cur_run:
            self.cur_run = int(row["id"])
            self.reload()

    # ---------- 数据 ----------
    def reload(self):
        self.listw.clear()
        self._flows.clear()
        self._runs.clear()
        self._trig.clear()
        if not self.win.root:
            return
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

        seg = self.seg.currentItem()
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
            self.listw.addItem(f"读取失败：{e}")
            return
        if not rows:
            self.listw.addItem("（还没有流程模板）")
            return
        for p in rows:
            tag = "内置" if int(p.get("builtin") or 0) else "自定义"
            self._add(f"[{tag}] {p['name']} — {p['title']}（{p['n_steps']} 步 · "
                      f"成功跑过 {p['n_done']} 次 · 最近：{p.get('last_status') or '未跑过'}）",
                      self._flows, p)

    def _load_steps(self):
        if not self.cur_flow:
            self.listw.addItem("（先在「流程」段选中一条流程，再来看步骤）")
            return
        try:
            pb = brain.playbook_get(self.win.root, self.cur_flow)
        except Exception as e:  # noqa: BLE001
            self.listw.addItem(f"读取失败：{e}")
            return
        if not pb:
            self.listw.addItem(f"（流程「{self.cur_flow}」不存在）")
            return
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
            self.listw.addItem(f"读取失败：{e}")
            return
        if not rows:
            self.listw.addItem("（还没有执行记录。选中流程点「跑一次」）")
            return
        for r in rows:
            self._add(f"#{r['id']} {r['playbook']} [{r['status']}] {r['summary']}"
                      f"（{r['project'] or '无项目'} · {r['agent'] or '无'} · {r['started'][:16]}）",
                      self._runs, r)
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

    def _load_trig(self):
        try:
            rows = brain.trigger_list(self.win.root)
        except Exception as e:  # noqa: BLE001
            self.listw.addItem(f"读取失败：{e}")
            return
        if not rows:
            self.listw.addItem("（还没有触发器。用 hub_trigger action=add 建一条——默认停用）")
            return
        for t in rows:
            state = "启用" if int(t.get("enabled") or 0) else "停用"
            self._add(f"#{t['id']} [{state}] {t['name']}：{t['on_event']} → {t['action_type']}"
                      f"（冷却 {t['cooldown_min']} 分钟 · 已触发 {t['fire_count']} 次 · "
                      f"上次 {(t.get('last_fired') or '从未')[:16]}）", self._trig, t)

    def _load_graph(self):
        """图谱段：体检一行 + 最大几个知识簇 + 孤岛清单（能看才能行动）。"""
        try:
            st = brain.graph_stats(self.win.root, build=False)
        except Exception as e:  # noqa: BLE001
            self.listw.addItem(f"读取失败：{e}")
            return
        bk = "、".join(f"{k} {v}" for k, v in (st.get("by_kind") or {}).items()) or "无"
        head = QListWidgetItem(
            f"── 边 {st.get('edges', 0)} 条（{bk}）｜已连条目 {st.get('linked', 0)}"
            f" · 孤岛 {st.get('isolated', 0)} · 最大簇 {st.get('largest_cluster', 0)}")
        head.setFlags(Qt.ItemFlag.NoItemFlags)
        self.listw.addItem(head)
        try:
            cl = brain.graph_clusters(self.win.root, limit=5, build=False)
        except Exception:  # noqa: BLE001
            cl = []
        if cl:
            c = QListWidgetItem("知识簇（规模降序）：")
            c.setFlags(Qt.ItemFlag.NoItemFlags)
            self.listw.addItem(c)
            for g in cl:
                self.listw.addItem(f"  {g['size']} 个条目：" + "；".join((g.get("sample") or [])[:2]))
        try:
            iso = brain.graph_isolated(self.win.root, limit=15)
        except Exception:  # noqa: BLE001
            iso = []
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
        if not cl and not iso:
            self.listw.addItem("（图谱还没有成规模的簇与孤岛）")

    def _selected(self):
        it = self.listw.currentItem()
        if it is None:
            return None
        seg = self.seg.currentItem()
        store = {"flows": self._flows, "runs": self._runs, "trig": self._trig}.get(seg)
        return store.get(id(it)) if store else None

    # ---------- 操作 ----------
    def run_flow(self):
        if not self.win.root:
            return
        row = self._selected()
        if self.seg.currentItem() != "flows" or not row:
            InfoBar.warning("先选中一条流程", "切到「流程」段并选中要跑的流程", parent=self)
            return
        name = row["name"]
        proj = self.projEdit.text().strip()
        if not proj:
            proj, ok = QInputDialog.getText(self, "跑流程", f"项目名（{{{{project}}}}，可空）：")
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
        if self.seg.currentItem() != "runs" or not row:
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
        if self.seg.currentItem() != "runs" or not row:
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
        if self.seg.currentItem() != "trig" or not row:
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
