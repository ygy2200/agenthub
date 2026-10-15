# -*- coding: utf-8 -*-
"""SearchPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
class SearchPage(QWidget):
    KIND_CN = {"record": "记录", "memory": "记忆", "file": "文件", "evfile": "全盘文件"}

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        set_page_margins(lay)
        lay.addWidget(_page_head("搜索", "全脑检索 + 全盘文件——回车执行，双击打开"))
        top = QHBoxLayout()
        self.edit = SearchLineEdit()
        self.edit.setPlaceholderText("全脑检索：工作记录 / 记忆 / 文件名，回车执行…")
        self.edit.setClearButtonEnabled(True)
        top.addWidget(self.edit, 1)
        self.kindFilter = ComboBox()
        self.kindFilter.addItems(["全部", "记录", "记忆", "文件", "全盘文件"])
        self.kindFilter.setFixedWidth(110)
        self.kindFilter.currentIndexChanged.connect(self.refill)
        top.addWidget(self.kindFilter)
        lay.addLayout(top)
        lay.addSpacing(tokens.SPACE_S)
        lay.addWidget(section_head("检索结果", "SEARCH"))

        # 左列表右预览可拖分栏：原预览固定 220px 高压在底部，列表只剩几行
        split = QSplitter(Qt.Horizontal)
        self.result = ListWidget()
        split.addWidget(self.result)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(tokens.SPACE_S)
        rv.addWidget(section_head("内容预览", "DOCUMENT"))
        act = QHBoxLayout()
        act.addStretch(1)
        self.btnOpen = PushButton(ic("LINK", "INFO"), "打开")
        self.btnOpen.clicked.connect(self.open_current)
        act.addWidget(self.btnOpen)
        self.btnLoc = PushButton(ic("FOLDER", "INFO"), "打开所在位置")
        self.btnLoc.clicked.connect(self.open_location_current)
        act.addWidget(self.btnLoc)
        self.btnJump = PushButton(ic("HOME", "INFO"), "跳到项目")
        self.btnJump.clicked.connect(self.jump_project)
        act.addWidget(self.btnJump)
        self.btnCopy = PushButton(ic("COPY", "INFO"), "复制内容")
        self.btnCopy.clicked.connect(self.copy_current)
        act.addWidget(self.btnCopy)
        rv.addLayout(act)
        self.preview = TextBrowser()
        rv.addWidget(self.preview, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
        split.setSizes([520, 420])
        lay.addWidget(split, 1)
        self.lay = lay        # Round 5：run() 里定位空态要用（局部变量在方法外不可见）
        self.split = split

        self.hits = []       # 本地命中（kind, data）
        self.view_hits = []  # 当前过滤下显示的命中
        self.ev = {"results": [], "error": ""}  # Everything 全盘段（异步补充）
        self.ev_for = ""
        self._workers = []  # 持住运行中线程引用，防 GC 崩进程
        self.edit.returnPressed.connect(self.run)
        self.result.currentRowChanged.connect(self.show_hit)
        self.result.itemDoubleClicked.connect(lambda _: self.open_current())
        # Round 5：run() 只在用户回车时才跑，启动时也要有引导（助手幂等，run() 再调只会显隐）
        list_empty_guard(self.result, lay,
                         "输入关键词开始检索",
                         "回车执行：全脑检索（记录 + 记忆）+ 全盘文件；"
                         "支持分类筛选与「全部」视图",
                         index=lay.indexOf(split))

    # ---- 数据
    def run(self):
        kw = self.edit.text().strip()
        if not kw or not self.win.root:
            return
        # 本地三查询是毫秒级 SQLite（实测 8/2/1ms），同步跑比 QThread 启动+信号往返更快——
        # 回车立即出结果，不再有"搜索中…"的顿挫
        try:
            res = brain.search_all(self.win.root, kw)
        except Exception as e:  # noqa: BLE001
            res = e
        self.on_done(res)

    def on_done(self, res):
        """本地结果同步填充；Everything 的 es.exe 子进程启动慢，丢后台线程异步追加全盘段。"""
        self.preview.setHtml("")
        self.ev = {"results": [], "error": ""}
        if isinstance(res, Exception):
            self.hits = [("info", f"搜索失败：{res}")]
        else:
            hits = []
            for r in res.get("records", []):
                hits.append(("record", r))
            for m in res.get("memories", []):
                hits.append(("memory", m))
            for f in res.get("files", []):
                hits.append(("file", f))
            self.hits = hits or [("info", "无结果")]
        self.refill()
        kw = self.edit.text().strip()
        self.ev_for = kw
        w = FnWorker(lambda: core.everything_search(kw, 20), self)
        w.done.connect(self._on_everything)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _on_everything(self, ev):
        if self.edit.text().strip() != self.ev_for:
            return  # 用户已改搜索词，这轮全盘结果作废
        self.ev = ev if isinstance(ev, dict) else {"results": [], "error": str(ev)}
        self.refill()

    def refill(self):
        """按类型过滤重建列表。本地在 self.hits，Everything 在 self.ev，显示子集在 self.view_hits。"""
        kind = self.kindFilter.currentIndex()  # 0全部 1记录 2记忆 3文件 4全盘
        key = {1: "record", 2: "memory", 3: "file", 4: "evfile"}.get(kind)
        self.result.clear()
        self.view_hits = []
        if key in (None, "evfile"):
            # Everything 全盘段：es.exe 异步回来才有内容，失败也给一行原因
            if key == "evfile" and self.ev.get("error"):
                self.result.addItem(f"（全盘：{self.ev['error']}）")
                self.view_hits.append(("info", None))
            for pth in self.ev.get("results", []):
                self.result.addItem(f"[全盘] {pth}")
                self.view_hits.append(("evfile", pth))
            if key is None and self.ev.get("error"):
                self.result.addItem(f"── 全盘文件：{self.ev['error']} ──")
                self.view_hits.append(("info", None))
        for k, h in self.hits:
            if key and k != key:
                continue
            if k == "info":
                if key is None:  # 失败/无结果提示只在"全部"里出现
                    self.result.addItem(h)
                    self.view_hits.append(("info", h))
            elif k == "record":
                src = (f"（图关联自{h['_cross'][2:]}）" if h.get("_cross")
                       else "（图关联）" if "graph" in (h.get("_channels") or {}) else "")
                self.result.addItem(f"[记录] {h['date']} {h['project']}（{h['agent']}）：{h['title'][:80]}{src}")
                self.view_hits.append((k, h))
            elif k == "memory":
                src = (f"（图关联自{h['_cross'][2:]}）" if h.get("_cross")
                       else "（图关联）" if "graph" in (h.get("_channels") or {}) else "")
                conf = f" ⚠️疑似互斥#{'/#'.join(str(i) for i in h['_conflict_with'])}" if h.get("_conflict_with") else ""
                self.result.addItem(f"[记忆#{h['id']}] {brain.KIND_CN.get(h['kind'], h['kind'])}：{h['content'][:100]}{src}{conf}")
                self.view_hits.append((k, h))
            elif k == "file":
                self.result.addItem(f"[文件] {h['project']}\\{h['name']}")
                self.view_hits.append((k, h))
        if not self.view_hits and self.hits and self.hits[0][0] != "info":
            self.result.addItem(f"该分类下无结果（全部 {len(self.hits)} 条）——切「全部」查看其他类型")
        tight_rows(self.result)
        # Round 5：还没检索/无结果时给引导（原来是一片空白）。
        # 结果列表在 QSplitter 里（不是 layout 子项），故显式指定插到分栏之前。
        list_empty_guard(self.result, self.lay,
                         "输入关键词开始检索",
                         "回车执行：全脑检索（记录 + 记忆）+ 全盘文件；"
                         "支持分类筛选与「全部」视图",
                         index=self.lay.indexOf(self.split))

    def _current(self):
        row = self.result.currentRow()
        if 0 <= row < len(self.view_hits):
            return self.view_hits[row]
        return None, None

    # ---- 交互
    def show_hit(self, row):
        kind, h = self._current() if row >= 0 else (None, None)
        self._sync_actions(kind)
        self.preview.setHtml(self._preview_html(kind, h))
        self._highlight()

    def _preview_html(self, kind, h):
        if kind in (None, "info"):
            return f"<p style='color:{tokens.C(tokens.TEXT_3)}'>点选左侧条目查看详情</p>"
        if kind == "evfile":
            return md_to_html(f"**全盘文件（Everything 检索）**\n\n`{h}`")
        if kind == "record":
            return md_to_html(f"## {h['title']}\n{h['content']}")
        if kind == "memory":
            return md_to_html(f"**{brain.KIND_CN.get(h['kind'], h['kind'])}**"
                              f"{(' #' + h['tags']) if h['tags'] else ''}\n\n{h['content']}")
        return md_to_html(f"**文件**（{h['project']}）\n\n`{h['path']}`")

    def _sync_actions(self, kind):
        """操作按钮按结果类型显隐：能打开的才出现打开按钮，不让按钮灰着占地方。"""
        self.btnOpen.setVisible(kind in ("file", "evfile"))
        self.btnLoc.setVisible(kind in ("file", "evfile"))
        self.btnJump.setVisible(kind == "record")
        self.btnCopy.setVisible(kind in ("record", "memory"))

    def _highlight(self):
        """预览正文里高亮关键词（ExtraSelection 运行时叠加，不改 HTML 结构）。"""
        kw = self.edit.text().strip()
        sels = []
        if kw:
            doc = self.preview.document()
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(255, 214, 90))
            cur = QTextCursor(doc)
            while True:
                cur = doc.find(kw, cur)
                if cur.isNull():
                    break
                sel = QTextEdit.ExtraSelection()
                sel.format = fmt
                sel.cursor = cur
                sels.append(sel)
        self.preview.setExtraSelections(sels)

    def open_current(self):
        kind, h = self._current()
        if kind in ("file", "evfile"):
            p = h["path"] if kind == "file" else h
            if p and Path(p).exists():
                os.startfile(p)
            else:
                InfoBar.warning("文件不存在", str(p)[:120], duration=3000, parent=self.win)
        elif kind == "record":
            self._open_record(h)

    def open_location_current(self):
        kind, h = self._current()
        if kind == "file":
            open_location(h["path"])
        elif kind == "evfile" and Path(h).exists():
            open_location(h)

    def _open_record(self, r):
        """记录落地文件：项目目录下的工作记录.md，没有则打开项目文件夹。"""
        base = Path(self.win.root) / r["project"]
        rec = base / core.RECORD_NAME
        if not self.win.root or not base.exists():
            InfoBar.warning("项目目录不存在", r["project"], duration=3000, parent=self.win)
            return
        os.startfile(rec if rec.is_file() else base)

    def jump_project(self):
        kind, h = self._current()
        if kind == "record" and hasattr(self.win, "project_page"):
            self.win.project_page.select_project(h["project"])
            self.win.switchTo(self.win.project_page)

    def copy_current(self):
        kind, h = self._current()
        if kind == "memory":
            QApplication.clipboard().setText(h["content"])
            InfoBar.success("已复制记忆内容", f"#{h['id']}", duration=2000, parent=self.win)
        elif kind == "record":
            QApplication.clipboard().setText(h["content"])
            InfoBar.success("已复制记录正文", h["title"][:60], duration=2000, parent=self.win)

