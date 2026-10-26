# -*- coding: utf-8 -*-
"""ProjectPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

import html as _html

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
        _outer.setSpacing(tokens.SPACE_M)
        _outer.addWidget(_page_head("项目", "每个项目的进展与工作记录——左选右看；双击列表项打开目录"))
        root = QHBoxLayout()
        _outer.addLayout(root, 1)

        left = QVBoxLayout()
        self.left = left  # Round 5：留着给 list_empty_guard 定位空态
        self.filterEdit = SearchLineEdit()
        self.filterEdit.setPlaceholderText("过滤项目…")
        self.listw = ListWidget()
        left.addWidget(section_head("项目列表", "FOLDER"))
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

        self.recCount = CaptionLabel("")
        self.recCount.setProperty("keepWidth", True)
        self.recCombo = ComboBox()
        self.recCombo.setPlaceholderText("跳转到记录…")
        self.recCombo.setMinimumWidth(240)
        self.recCombo.setMaximumWidth(380)
        self.recCombo.setEnabled(False)
        recHead = QHBoxLayout()
        recHead.addWidget(section_head("工作记录", "NOTE"))
        recHead.addStretch(1)
        recHead.addWidget(self.recCount)
        recHead.addWidget(self.recCombo)

        self.browser = TextBrowser()
        self.browser.setHtml(f"<p style='color:{tokens.C(tokens.TEXT_3)}'>左侧选择项目查看工作记录</p>")

        file_col = QVBoxLayout()
        self.fileHead = section_head("项目文件总览", "DOCUMENT")
        file_col.addWidget(self.fileHead)
        self.fileCap = CaptionLabel("历史项目无 input/output 时按实际位置分组 · 双击打开所在位置")
        file_col.addWidget(self.fileCap)
        self.fileList = ListWidget()
        file_col.addWidget(self.fileList, 1)
        self.fileList.itemDoubleClicked.connect(self.open_file_loc)

        self.nameLabel = PageTitleLabel("—")   # 主实体名与页头同级 24
        # （原 TitleLabel 28 —— 比 24 的页头还大，层级倒挂）
        self.nameLabel.setWordWrap(True)  # 长项目名换行（防右下详情列被标题撑宽，2026-10-10 溢出体检）
        right.addWidget(self.nameLabel)
        right.addLayout(head)
        right.addWidget(self.pathLabel)
        right.addWidget(self.issueLabel)
        right.addLayout(recHead)
        right.addWidget(self.browser, 3)
        right.addLayout(file_col, 1)  # 文件总览也参与伸缩：窗口矮时说明行不再被裁出可视区

        root.addLayout(left, 1)
        root.addLayout(right, 2)
        self.right = right          # R196：记录区空态要插在这里
        self._recEmpty = None

        self.listw.currentRowChanged.connect(self.on_select)
        self.listw.itemDoubleClicked.connect(lambda _item: self.open_dir())
        self.filterEdit.textChanged.connect(self.rebuild_list)
        self.recCombo.activated.connect(self.jump_record)
        self.openBtn.clicked.connect(self.open_dir)
        self.copyBtn.clicked.connect(self.copy_path)
        self.newBtn.clicked.connect(self.new_project)
        self.inboxBtn.clicked.connect(self.pick_inbox)
        self.refreshBtn.clicked.connect(self.win.refresh)
        self._sync_actions()

    # ---- 数据
    def _show_rec_empty(self):
        """空库时的唯一引导（R196）：放在**宽的那一栏**（记录区），带 CTA。

        原来左窄栏挂空态、记录区再写一句「还没有项目：点左下新建…」，同一件事说两遍，
        左栏 300px 里四行字还折得七零八落；而「新建项目」按钮在最左下角，离提示很远。
        """
        if self._recEmpty is None:
            self._recEmpty = empty_state(
                "HISTORY", "还没有任何项目",
                "点下面的「新建项目」建第一个；也可以让 agent 说一句「新建项目 名字」，它会写进大脑",
                action_text="新建项目", action=self.new_project)
            idx = self.right.indexOf(self.browser)
            self.right.insertWidget(idx if idx >= 0 else self.right.count() - 1, self._recEmpty, 3)
        self._recEmpty.setVisible(True)
        self.browser.setVisible(False)
        # 没有项目时「项目文件总览」无内容可看，还占掉一半右栏高度——空态只能拿到
        # 119px（需要 169px），按钮被压成 20px 高的扁条、说明被裁一行（雅黑实测）。
        for w in (self.fileHead, self.fileCap, self.fileList):
            w.setVisible(False)

    def _hide_rec_empty(self):
        if self._recEmpty is not None:
            self._recEmpty.setVisible(False)
        self.browser.setVisible(True)
        for w in (self.fileHead, self.fileCap, self.fileList):
            w.setVisible(True)

    def _sync_actions(self):
        """无选中项目时收起「打开目录/复制路径/从 Inbox 引入」：
        三个动作都依赖 self.current，亮着点下去只会静默无反应（R192 空库截图）。"""
        has = bool(self.current)
        for b in (self.inboxBtn, self.copyBtn, self.openBtn):
            b.setEnabled(has)

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
        else:
            self.current = None
            # R196：占位符「—」用 24px 标题字号画出来就是右栏顶部一条孤立短横线，
            # 像分隔线漏了半个；无选中时直接不显示标题。
            self.nameLabel.setVisible(False)
            self.nameLabel.setText("")
            self.pathLabel.setText("")
            self.issueLabel.setText("")
            self.fileList.clear()
            self._fill_records([], 0)
            # 不再在文件区挂大空态：920x600 下右栏只剩 ~58px，空态反而被裁成半截图标。
            # 记录区文案区分「库空」与「过滤无命中」。
            if getattr(self, "db_projects", []):
                self._hide_rec_empty()
                tip = "左侧选择项目查看工作记录"
                self.browser.setHtml(f"<p style='color:{tokens.C(tokens.TEXT_3)}'>{tip}</p>")
            else:
                self._show_rec_empty()
        self._sync_actions()
        # Round 5：空库/过滤无命中时给引导（原来是一片空白）
        if kw:
            list_empty_guard(self.listw, self.left,
                             f"没有匹配「{kw}」的项目",
                             "换个关键词，或清空过滤框看全部项目")
        else:
            hint = list_empty_guard(self.listw, self.left, "项目列表为空", "")
            # 库空时的唯一引导在记录区（带 CTA）；左栏再挂一份就是同义重复
            hint.setVisible(False)

    def on_select(self, row):
        if row < 0 or row >= self.listw.count():
            return
        self._hide_rec_empty()
        name = self.listw.item(row).text().rsplit("   (", 1)[0]
        proj_scan = next((p for p in self.snap.projects if p.name == name), None) if self.snap else None
        self.current = name
        self.nameLabel.setVisible(True)
        self.nameLabel.setText(name)
        self.pathLabel.setText(str(Path(self.win.root) / name))
        issues = proj_scan.issues if proj_scan else []
        self.issueLabel.setText("！" + "；".join(issues) if issues else "")
        # 工作记录正文：大脑数据库
        try:
            total = brain.records_count(self.win.root, project=name)
            recs = brain.list_records(self.win.root, project=name, limit=500)
        except Exception:
            total, recs = 0, []
        if recs:
            # R119：最新在上——list_records 本就 date DESC，原 reversed() 让预览从
            # 最旧记录开始（实测项目页顶部是 2026-09-27，看进展要滚 500 条）。
            # 「读项目历史」场景点开任一条目的完整顺序在时间线页，这里默认给现状。
            # R168：逐条渲染（原来整段拼接后 md_to_html 默认 100KB 上限，359 条项目
            # 只显示前 ~100 条且无提示）；每条加锚点供下拉跳转。
            self._fill_records(recs, total)
            self.browser.setHtml(self._records_html(recs))
        elif proj_scan and proj_scan.doc_path:
            self._fill_records([], 0, fallback="无大脑记录 · 显示目录文档")
            self.browser.setHtml(md_to_html(core.read_text(Path(proj_scan.doc_path))))
        else:
            self._fill_records([], 0)
            self.browser.setHtml(f"<p style='color:{tokens.C(tokens.TEXT_3)}'>大脑中无此项目记录，目录中也无可用主文档</p>")
        self.fileList.clear()
        if proj_scan:
            for group, rp, fp in proj_scan.files:
                self.fileList.addItem(f"[{group}]  {rp}")
                self.fileList.item(self.fileList.count() - 1).setData(Qt.UserRole, fp)
            if not proj_scan.files:
                self.fileList.addItem("（空目录）")
        # 有文件时要收起旧空态（原实现只在未选中时调用，「暂未选中项目」会一直压在
        # 文件列表上）；「（空目录）」占一行，属有内容。文件区主文案在上方记录区。
        self._sync_actions()

    def _fill_records(self, recs, total=0, fallback: str = ""):
        self.recCombo.blockSignals(True)
        self.recCombo.clear()
        self.recCombo.setCurrentIndex(-1)
        if not recs:
            self.recCount.setText(fallback or "共 0 条")
            self.recCombo.setEnabled(False)
            self.recCombo.blockSignals(False)
            return
        for i, r in enumerate(recs):
            title = " ".join(str(r.get("title") or "（无标题）").split())
            d = str(r.get("date") or "")
            label = f"{d} · {title}" if d else title
            self.recCombo.addItem(label if len(label) <= 52 else label[:51] + "…")
            self.recCombo.setItemData(i, {"anchor": f"rec-{i}", "full": label})
        if total > len(recs):
            self.recCount.setText(f"共 {total} 条 · 显示最近 {len(recs)} 条")
        else:
            self.recCount.setText(f"共 {len(recs)} 条")
        self.recCombo.setEnabled(True)
        self.recCombo.setCurrentIndex(-1)  # qfluentwidgets 插入首项会自动选中 0
        self.recCombo.blockSignals(False)

    def _records_html(self, recs) -> str:
        parts: list[str] = []
        for i, r in enumerate(recs):
            raw_title = " ".join(str(r.get("title") or "（无标题）").split())
            title = _html.escape(raw_title)
            d = str(r.get("date") or "")
            agent = str(r.get("agent") or "")
            meta = " · ".join(x for x in (d, agent) if x)
            parts.append(f'<a name="rec-{i}"></a><h4>{title}</h4>')
            # 多数记录标题已带日期/agent；重复的日期行只会稀释信息
            if meta and not (d and d in raw_title):
                parts.append(f"<p style='color:{tokens.C(tokens.TEXT_3)}'>{_html.escape(meta)}</p>")
            parts.append(md_to_html(r.get("content") or ""))
        return "\n".join(parts)

    def jump_record(self, index):
        data = self.recCombo.itemData(index) or {}
        anchor = data.get("anchor") if isinstance(data, dict) else ""
        if not anchor:
            return
        self.browser.scrollToAnchor(anchor)
        self.browser.setFocus()
        # 跳转是一次性动作：回到占位提示，方便再次选择同一条
        self.recCombo.blockSignals(True)
        self.recCombo.setCurrentIndex(-1)
        self.recCombo.blockSignals(False)

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

