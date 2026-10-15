# -*- coding: utf-8 -*-
"""ProjectPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
class ProjectPage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.snap = None
        self.current = None
        _outer = QVBoxLayout(self)
        set_page_margins(_outer)
        _outer.setSpacing(12)
        _outer.addWidget(_page_head("项目", "每个项目的进展与工作记录——左选右看；双击列表项打开目录"))
        root = QHBoxLayout()
        _outer.addLayout(root, 1)

        left = QVBoxLayout()
        self.left = left  # Round 5：留着给 list_empty_guard 定位空态
        self.filterEdit = SearchLineEdit()
        self.filterEdit.setPlaceholderText("过滤项目…")
        self.listw = ListWidget()
        left.addWidget(self.filterEdit)
        left.addWidget(self.listw, 1)
        btnrow = QHBoxLayout()
        self.newBtn = PrimaryPushButton(ic("ADD", "INFO"), "新建项目")
        self.refreshBtn = PushButton(ic("SYNC", "INFO"), "刷新")
        btnrow.addWidget(self.newBtn)
        btnrow.addWidget(self.refreshBtn)
        left.addLayout(btnrow)

        right = QVBoxLayout()
        head = QHBoxLayout()
        head.addStretch(1)
        self.openBtn = PushButton(ic("FOLDER", "INFO"), "打开目录")
        self.copyBtn = PushButton(ic("COPY", "INFO"), "复制路径")
        self.inboxBtn = PushButton(ic("DOWNLOAD", "INFO"), "从 Inbox 引入")
        for b in (self.inboxBtn, self.copyBtn, self.openBtn):
            head.addWidget(b)
        self.pathLabel = CaptionLabel("")
        self.issueLabel = BodyLabel("")
        # Round 3（配色逻辑）：审计问题是「警告」性质（目录命名不规范/缺记录，软件只报告不自动改），
        # 原来用 DANGER（红=失败/危险）过强且语义不符；改用 WARNING（琥珀）。
        # 这也是 tokens.WARNING 的首次真正使用——此前该语义色定义后全库 0 引用。
        self.issueLabel.setStyleSheet(f"color:{tokens.C(tokens.WARNING)};")

        self.browser = TextBrowser()
        self.browser.setHtml(f"<p style='color:{tokens.C(tokens.TEXT_3)}'>左侧选择项目查看工作记录</p>")

        file_col = QVBoxLayout()
        self.file_col = file_col  # Round 5：同上
        file_col.addWidget(CaptionLabel("项目文件总览（历史项目无 input/output 时按实际位置分组 · 双击打开所在位置）"))
        self.fileList = ListWidget()
        file_col.addWidget(self.fileList, 1)
        self.fileList.itemDoubleClicked.connect(self.open_file_loc)

        self.nameLabel = TitleLabel("—")
        self.nameLabel.setWordWrap(True)  # 长项目名换行（防右下详情列被标题撑宽，2026-10-10 溢出体检）
        right.addWidget(self.nameLabel)
        right.addLayout(head)
        right.addWidget(self.pathLabel)
        right.addWidget(self.issueLabel)
        right.addWidget(self.browser, 1)
        right.addLayout(file_col, 1)  # 文件总览也参与伸缩：窗口矮时说明行不再被裁出可视区

        root.addLayout(left, 1)
        root.addLayout(right, 2)

        self.listw.currentRowChanged.connect(self.on_select)
        self.filterEdit.textChanged.connect(self.rebuild_list)
        self.openBtn.clicked.connect(self.open_dir)
        self.copyBtn.clicked.connect(self.copy_path)
        self.newBtn.clicked.connect(self.new_project)
        self.inboxBtn.clicked.connect(self.pick_inbox)
        self.refreshBtn.clicked.connect(self.win.refresh)

    # ---- 数据
    def set_snapshot(self, snap):
        self.snap = snap
        try:
            self.db_projects = brain.list_projects(self.win.root, 200)
        except Exception:
            self.db_projects = []
        selected = self.current
        names = [p["name"] for p in self.db_projects]
        self.rebuild_list()
        if selected in names:
            self.listw.setCurrentRow(names.index(selected))
        elif self.listw.count():
            self.listw.setCurrentRow(0)

    def rebuild_list(self):
        kw = self.filterEdit.text().strip().lower()
        self.listw.blockSignals(True)
        self.listw.clear()
        for p in getattr(self, "db_projects", []):
            if kw in p["name"].lower():
                n = p.get("n_records", 0)
                self.listw.addItem(f"{p['name']}   ({n}条)")
        self.listw.blockSignals(False)
        tight_rows(self.listw)
        if self.listw.count():
            self.listw.setCurrentRow(0)
        # Round 5：空库/过滤无命中时给引导（原来是一片空白）
        if kw:
            list_empty_guard(self.listw, self.left,
                             f"没有匹配「{kw}」的项目",
                             "换个关键词，或清空过滤框看全部项目")
        else:
            list_empty_guard(self.listw, self.left,
                             "还没有任何项目",
                             "点「新建项目」创建第一个项目，或让 agent 调 hub_create_project")

    def on_select(self, row):
        if row < 0 or row >= self.listw.count():
            return
        name = self.listw.item(row).text().rsplit("   (", 1)[0]
        proj_scan = next((p for p in self.snap.projects if p.name == name), None) if self.snap else None
        self.current = name
        self.nameLabel.setText(name)
        self.pathLabel.setText(str(Path(self.win.root) / name))
        issues = proj_scan.issues if proj_scan else []
        self.issueLabel.setText("⚠ " + "；".join(issues) if issues else "")
        # 工作记录正文：大脑数据库
        try:
            recs = brain.list_records(self.win.root, project=name, limit=500)
        except Exception:
            recs = []
        if recs:
            md = "\n\n".join(f"## {r['title']}\n{r['content']}" for r in reversed(recs))
            self.browser.setHtml(md_to_html(md))
        elif proj_scan and proj_scan.doc_path:
            self.browser.setHtml(md_to_html(core.read_text(Path(proj_scan.doc_path))))
        else:
            self.browser.setHtml(f"<p style='color:{tokens.C(tokens.TEXT_3)}'>大脑中无此项目记录，目录中也无可用主文档</p>")
        self.fileList.clear()
        if proj_scan:
            for group, rp, fp in proj_scan.files:
                self.fileList.addItem(f"[{group}]  {rp}")
                self.fileList.item(self.fileList.count() - 1).setData(Qt.UserRole, fp)
            if not proj_scan.files:
                self.fileList.addItem("（空目录）")
        # Round 5：没选中项目时给引导（原来是空白）
        if not proj_scan:
            list_empty_guard(self.fileList, self.file_col,
                             "暂未选中项目",
                             "选中左侧项目后，这里列出它的 input/output 等文件")

    def open_file_loc(self, item):
        fp = item.data(Qt.UserRole)
        if fp:
            open_location(fp)

    def open_dir(self):
        if self.current:
            p = Path(self.win.root) / self.current
            if p.is_dir():
                os.startfile(str(p))  # noqa: S606

    def copy_path(self):
        if self.current:
            QApplication.clipboard().setText(str(Path(self.win.root) / self.current))
            InfoBar.success("已复制", "", duration=1500, parent=self.win)

    def new_project(self):
        if not self.win.root:
            InfoBar.warning("未设置根目录", "请先到设置页选择", duration=2500, parent=self.win)
            return
        dlg = NewProjectDialog(self.win)
        if dlg.exec():
            err = core.create_project(self.win.root, dlg.name())
            if err:
                InfoBar.error("创建失败", err, duration=4000, parent=self.win)
            else:
                InfoBar.success("已创建", dlg.name(), duration=2000, parent=self.win)
                self.win.refresh()

    def pick_inbox(self):
        if not self.current or not self.snap:
            return
        files = core.scan(self.win.root).inbox if self.win.root else []
        if not files:
            InfoBar.info("00_Inbox 是空的", "", duration=2000, parent=self.win)
            return
        dlg = InboxPickDialog(self.win, files)
        if dlg.exec():
            errs = [e for e in (core.move_to_project(self.win.root, f, self.current) for f in dlg.picked()) if e]
            if errs:
                InfoBar.error("部分失败", errs[0], duration=4000, parent=self.win)
            else:
                InfoBar.success("已引入", "", duration=2000, parent=self.win)
                self.win.refresh()

    def select_project(self, name):
        names = [self.listw.item(i).text() for i in range(self.listw.count())]
        if name in names:
            self.listw.setCurrentRow(names.index(name))

