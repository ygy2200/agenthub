# -*- coding: utf-8 -*-
"""SearchPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
class SearchPage(QWidget):
    KIND_CN = {"record": "记录", "memory": "记忆", "note": "随记", "task": "任务",
               "error": "错误", "file": "文件", "evfile": "全盘文件"}
    EMPTY_TITLE = "输入关键词开始检索"
    EMPTY_DESC = "试试项目名、错误现象或记忆里的关键词；回车执行，结果里可直接预览/打开/跳转。"
    EMPTY_ACTION = "搜索最近项目"

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        lay = QVBoxLayout(self)
        set_page_margins(lay)
        lay.addWidget(_page_head("搜索", "全脑检索 + 全盘文件——回车执行，双击或 Enter 打开"))
        top = QHBoxLayout()
        self.edit = SearchLineEdit()
        self.edit.setPlaceholderText("全脑检索：记录 / 记忆 / 随记 / 任务 / 错误 / 文件名，回车执行…")
        self.edit.setClearButtonEnabled(True)
        top.addWidget(self.edit, 1)
        self.kindFilter = ComboBox()
        self.kindFilter.addItems(["全部", "记录", "记忆", "随记", "任务", "错误", "文件", "全盘文件"])
        self.kindFilter.setFixedWidth(120)
        self.kindFilter.currentIndexChanged.connect(lambda _i: self.refill(auto_select=True))
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
        self.preview.setHtml(self._intro_html())
        rv.addWidget(self.preview, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
        split.setSizes([520, 420])
        lay.addWidget(split, 1)
        self.lay = lay        # Round 5：run() 里定位空态要用（局部变量在方法外不可见）
        self.split = split
        self.split.setVisible(False)  # R166：没有检索前不摆一条空列表+空预览（右栏大空白）

        self.hits = []       # 本地命中（kind, data）
        self.view_hits = []  # 当前过滤下显示的命中
        self.ev = {"results": [], "error": ""}  # Everything 全盘段（异步补充）
        self.ev_for = ""
        self._workers = []  # 持住运行中线程引用，防 GC 崩进程
        self.edit.returnPressed.connect(self.run)
        self.edit.textChanged.connect(self._on_text_changed)
        self.result.currentRowChanged.connect(self.show_hit)
        self.result.itemDoubleClicked.connect(lambda _: self.open_current())
        # R188 键盘路径：输入框 ↑/↓ 进结果列表、列表 Enter 打开、Esc 回输入框
        self.result.itemActivated.connect(lambda _: self.open_current())
        self.edit.installEventFilter(self)
        self.result.installEventFilter(self)
        # R166：未选中结果时右侧四个操作按钮不应全部亮着（点了静默无反应）
        self._sync_actions(None)
        # Round 5：run() 只在用户回车时才跑，启动时也要有引导（助手幂等，run() 再调只会显隐）
        list_empty_guard(self.result, lay, self.EMPTY_TITLE, self.EMPTY_DESC,
                         index=lay.indexOf(split),
                         action_text=self.EMPTY_ACTION, action=self.search_recent_project)

    # ---- 空态 / 清理
    def eventFilter(self, obj, e):
        if e.type() == QEvent.KeyPress:
            if obj is self.edit and e.key() in (Qt.Key_Down, Qt.Key_Up):
                if self._enter_results(1 if e.key() == Qt.Key_Down else -1):
                    return True
            elif obj is self.result and e.key() == Qt.Key_Escape:
                self.edit.setFocus()
                self.edit.selectAll()
                return True
        return super().eventFilter(obj, e)

    def _enter_results(self, step: int) -> bool:
        """输入框 ↑/↓：把焦点交给结果列表并移动选中（无选中时落到首/末条）。"""
        n = self.result.count()
        if n <= 0:
            return False
        row = self.result.currentRow()
        if row < 0:
            row = 0 if step > 0 else n - 1
        else:
            row = max(0, min(n - 1, row + step))
        self.result.setCurrentRow(row)
        self.result.setFocus()
        return True

    def _intro_html(self):
        return (f"<p style='color:{tokens.C(tokens.TEXT_3)}'>输入关键词后回车："
                "这里显示全文、关键词高亮与可执行操作。<br>"
                "键盘：输入框 ↑/↓ 进结果列表，Enter 打开，Esc 回搜索框；"
                "支持记录 / 记忆 / 随记 / 任务 / 错误 / 文件 / 全盘文件；"
                "Ctrl+F 随时回到搜索框。</p>")

    def search_recent_project(self):
        """空态 CTA：用最近一条记录的项目名起搜，给第一次打开搜索页的人一个起点。"""
        if not self.win.root:
            InfoBar.warning("未选择大脑目录", "先在接入页选择数据目录", parent=self.win)
            return
        try:
            rows = brain.list_records(self.win.root, limit=1)
        except Exception as e:  # noqa: BLE001
            op_failed("读取最近项目", e, parent=self)
            return
        if not rows:
            InfoBar.info("暂无工作记录", "先用 hub_log_work 或项目页写一条，再来搜索",
                         parent=self.win)
            return
        proj = (rows[0].get("project") or "").strip()
        if not proj:
            InfoBar.warning("最近记录没有项目名", "直接输入标题里的关键词试试", parent=self.win)
            return
        self.edit.setText(proj)
        self.edit.setFocus()
        self.run()

    def _on_text_changed(self, text):
        if not text.strip():
            self._clear_results()

    def _clear_results(self):
        """清空搜索框 = 回到初始态：旧结果、旧预览、悬空操作一并收起。"""
        self.ev_for = ""  # 让还在跑的全盘检索结果作废
        self.ev = {"results": [], "error": ""}
        self.hits, self.view_hits = [], []
        self.result.clear()
        self.split.setVisible(False)
        self._sync_actions(None)
        self.preview.setHtml(self._intro_html())
        list_empty_guard(self.result, self.lay, self.EMPTY_TITLE, self.EMPTY_DESC,
                         index=self.lay.indexOf(self.split),
                         action_text=self.EMPTY_ACTION, action=self.search_recent_project)

    # ---- 数据
    def run(self):
        kw = self.edit.text().strip()
        if not kw or not self.win.root:
            return
        # 本地六类查询都是毫秒级 SQLite，同步跑比 QThread 启动+信号往返更快——
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
        kw = self.edit.text().strip()
        if isinstance(res, Exception):
            self.hits = [("info", f"搜索失败：{res}")]
        else:
            hits = []
            for r in res.get("records", []):
                hits.append(("record", r))
            for m in res.get("memories", []):
                hits.append(("memory", m))
            for n in res.get("notes", []):
                hits.append(("note", n))
            for t in res.get("tasks", []):
                hits.append(("task", t))
            for e in res.get("errors", []):
                hits.append(("error", e))
            for f in res.get("files", []):
                hits.append(("file", f))
            self.hits = hits or [("info", f"未找到「{kw}」——本地六类 0 条")]
        self.refill(auto_select=True)
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

    def refill(self, *_args, auto_select=False):
        """按类型过滤重建列表。本地在 self.hits，Everything 在 self.ev，显示子集在 self.view_hits。

        auto_select=True：检索/切分类后自动选中首条（预览立刻有内容）；
        Everything 异步补全时保持用户当前选中项，不打断阅读。
        """
        prev = self._sel_key()
        kind = self.kindFilter.currentIndex()  # 0全部 1记录 2记忆 3随记 4任务 5错误 6文件 7全盘
        key = {1: "record", 2: "memory", 3: "note", 4: "task", 5: "error",
               6: "file", 7: "evfile"}.get(kind)
        self.result.clear()
        self.view_hits = []
        # 本地六类在前、全盘文件在后：大脑里的命中比磁盘路径更相关
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
                # R137：带 #id（与 MCP hub_search 同款精读链路）；标题经 _display_title
                # 清洗与空洞标题替代，去掉行头重复日期（title 本就含日期）。
                self.result.addItem(
                    f"[记录#{h['id']}] {_display_title(h)[:76]} · {h['project']}（{h['agent']}）{src}")
                self.view_hits.append((k, h))
            elif k == "memory":
                src = (f"（图关联自{h['_cross'][2:]}）" if h.get("_cross")
                       else "（图关联）" if "graph" in (h.get("_channels") or {}) else "")
                conf = f" ！疑似互斥#{'/#'.join(str(i) for i in h['_conflict_with'])}" if h.get("_conflict_with") else ""
                self.result.addItem(f"[记忆#{h['id']}] {brain.KIND_CN.get(h['kind'], h['kind'])}：{h['content'][:100]}{src}{conf}")
                self.view_hits.append((k, h))
            elif k == "note":
                mood = f"（{h['mood']}）" if h.get("mood") else ""
                self.result.addItem(f"[随记#{h['id']}] {h['ts'][:10]}{mood}：{h['content'][:100]}")
                self.view_hits.append((k, h))
            elif k == "task":
                st = brain.TASK_CN.get(h["status"], h["status"])
                who = h.get("owner") or "无主"
                due = f" ⏰{h['due'][:10]}" if h.get("due") else ""
                self.result.addItem(f"[任务#{h['id']}] [{st}] {h.get('project') or '—'}："
                                    f"{h['title'][:80]}（{who}）{due}")
                self.view_hits.append((k, h))
            elif k == "error":
                st = "未关闭" if h["status"] == "open" else "已修复"
                self.result.addItem(f"[错误#{h['id']}] [{st}] {h.get('project') or '—'}"
                                    f"（{h['agent']}）：{h['title'][:80]}")
                self.view_hits.append((k, h))
            elif k == "file":
                self.result.addItem(f"[文件] {h['project']}\\{h['name']}")
                self.view_hits.append((k, h))
        if key in (None, "evfile"):
            # Everything 全盘段：es.exe 异步回来才有内容，失败也给一行原因
            if key == "evfile" and self.ev.get("error"):
                msg = f"全盘搜索失败：{self.ev['error']}"
                self.result.addItem(f"（全盘：{self.ev['error']}）")
                self.view_hits.append(("info", msg))
            for pth in self.ev.get("results", []):
                self.result.addItem(f"[全盘] {pth}")
                self.view_hits.append(("evfile", pth))
            if key is None and self.ev.get("error"):
                msg = f"全盘文件：{self.ev['error']}"
                self.result.addItem(f"── {msg} ──")
                self.view_hits.append(("info", msg))
        if not self.view_hits and self.hits and self.hits[0][0] != "info":
            msg = f"该分类下无结果（全部 {len(self.hits)} 条）——切「全部」查看其他类型"
            self.result.addItem(msg)
            self.view_hits.append(("info", msg))
        tight_rows(self.result)
        self.split.setVisible(bool(self.view_hits))
        # 选中项按身份恢复（Everything 补全/切分类不丢）；新检索自动选首条
        idx = self._find_key(prev) if prev else -1
        if idx >= 0:
            self.result.setCurrentRow(idx)
        elif auto_select and self.view_hits:
            first_real = next((i for i, (k, _h) in enumerate(self.view_hits)
                               if k != "info"), 0)
            self.result.setCurrentRow(first_real)
        else:
            self.result.setCurrentRow(-1)
            self._sync_actions(None)
        # Round 5：还没检索/无结果时给引导（原来是一片空白）。
        # 结果列表在 QSplitter 里（不是 layout 子项），故显式指定插到分栏之前。
        list_empty_guard(self.result, self.lay, self.EMPTY_TITLE, self.EMPTY_DESC,
                         index=self.lay.indexOf(self.split),
                         action_text=self.EMPTY_ACTION, action=self.search_recent_project)

    @staticmethod
    def _key_of(kind, h):
        if kind is None:
            return None
        if kind == "info":
            return (kind, h or "")  # 提示行也保留选中，避免 Everything 补全时清掉说明
        if kind == "evfile":
            return (kind, h)
        if kind == "file":
            return (kind, h.get("path") or "")
        return (kind, h.get("id"))

    def _sel_key(self):
        kind, h = self._current()
        return self._key_of(kind, h)

    def _find_key(self, key):
        for i, (kind, h) in enumerate(self.view_hits):
            if self._key_of(kind, h) == key:
                return i
        return -1

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
        if kind is None:
            return self._intro_html()
        if kind == "info":
            tips = ["- 用更短的关键词，或换同义词"]
            if self.kindFilter.currentIndex() != 0:
                tips.append("- 切到「全部」分类，看看其他类型")
            tips.append("- 全盘文件由 Everything 异步补充，稍等再看")
            return md_to_html(f"**{h or '暂无结果'}**\n\n**可以试试**\n" + "\n".join(tips))
        if kind == "evfile":
            return md_to_html(f"**全盘文件（Everything 检索）**\n\n`{h}`")
        if kind == "record":
            return md_to_html(f"## {h['title']}\n{h['content']}")
        if kind == "memory":
            return md_to_html(f"**{brain.KIND_CN.get(h['kind'], h['kind'])}**"
                              f"{(' #' + h['tags']) if h['tags'] else ''}\n\n{h['content']}")
        if kind == "note":
            mood = f"（{h['mood']}）" if h.get("mood") else ""
            tags = f" #{h['tags']}" if h.get("tags") else ""
            return md_to_html(f"**随记 #{h['id']}** {h['ts'][:10]}{mood}{tags}\n\n{h['content']}")
        if kind == "task":
            st = brain.TASK_CN.get(h["status"], h["status"])
            body = (f"## {h['title']}\n\n**状态** {st} · **负责人** {h.get('owner') or '无主'}"
                    f" · **项目** {h.get('project') or '—'}")
            if h.get("due"):
                body += f" · **截止** {h['due']}"
            if h.get("detail"):
                body += f"\n\n{h['detail']}"
            if h.get("last_error"):
                body += f"\n\n**最近错误** {h['last_error']}"
            return md_to_html(body)
        if kind == "error":
            st = "未关闭" if h["status"] == "open" else "已修复"
            body = (f"## {h['title']}\n\n**状态** {st} · **项目** {h.get('project') or '—'}"
                    f" · **登记** {h['agent']} {h['ts'][:10]}")
            if h.get("detail"):
                body += f"\n\n{h['detail']}"
            if h.get("undo"):
                body += f"\n\n**回滚方式** {h['undo']}"
            return md_to_html(body)
        return md_to_html(f"**文件**（{h['project']}）\n\n`{h['path']}`")

    def _sync_actions(self, kind):
        """操作按钮按结果类型显隐：能打开的才出现打开按钮，不让按钮灰着占地方。"""
        self.btnOpen.setVisible(kind in ("file", "evfile"))
        self.btnLoc.setVisible(kind in ("file", "evfile"))
        self.btnJump.setVisible(kind in ("record", "task"))
        self.btnCopy.setVisible(kind in ("record", "memory", "note", "task", "error"))

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
        if kind in ("record", "task") and h.get("project") and hasattr(self.win, "project_page"):
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
        elif kind == "note":
            QApplication.clipboard().setText(h["content"])
            InfoBar.success("已复制随记内容", f"#{h['id']}", duration=2000, parent=self.win)
        elif kind == "task":
            QApplication.clipboard().setText(f"{h['title']}\n\n{h.get('detail') or ''}".strip())
            InfoBar.success("已复制任务内容", f"#{h['id']}", duration=2000, parent=self.win)
        elif kind == "error":
            txt = f"{h['title']}\n\n{h.get('detail') or ''}"
            if h.get("undo"):
                txt += f"\n\n回滚：{h['undo']}"
            QApplication.clipboard().setText(txt.strip())
            InfoBar.success("已复制错误记录", f"#{h['id']}", duration=2000, parent=self.win)

