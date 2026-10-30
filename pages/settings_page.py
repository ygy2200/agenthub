# -*- coding: utf-8 -*-
"""SettingsPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
import os
import ops
import brain


class SettingsPage(QWidget):
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
        lay.addWidget(_page_head("设置", "根目录 / 数据库 / 规则 / 外观 / 导航"))
        lay.addSpacing(4)

        c0 = CardWidget()
        g0 = QVBoxLayout(c0)
        g0.setContentsMargins(tokens.PAD_CARD_WIDE, tokens.PAD_CARD,
                                tokens.PAD_CARD_WIDE, tokens.PAD_CARD)
        g0.addWidget(section_head("外观", "PALETTE"))
        themeRow = QHBoxLayout()
        self._themeBtns = {}
        for _m in tokens.THEME_MODES:
            _b = PushButton(tokens.MODE_CN[_m])
            _b.clicked.connect(lambda _, mm=_m: self._on_theme_pick(mm))
            themeRow.addWidget(_b)
            self._themeBtns[_m] = _b
        themeRow.addStretch(1)
        g0.addLayout(themeRow)

        # 密实度（P4：长会话工具偏紧凑，但也给舒适档——行高与间距缩放联动）
        densRow = QHBoxLayout()
        densRow.setSpacing(tokens.SPACE_S)
        densRow.addWidget(CaptionLabel("密实度"))
        self._densBtns = {}
        for _k, _cn in (("compact", "紧凑 32px"), ("basic", "标准 36px"),
                        ("comfortable", "舒适 44px")):
            _b = PushButton(_cn)
            _b.clicked.connect(lambda _, kk=_k: self._on_density_pick(kk))
            densRow.addWidget(_b)
            self._densBtns[_k] = _b
        densRow.addStretch(1)
        g0.addLayout(densRow)

        # 动效开关（关掉后 hover 等反馈瞬时切换，适合低性能设备/无障碍场景）
        self.motionBox = CheckBox("界面动效（hover 淡入 200ms）")
        self.motionBox.setChecked(tokens.motion_ms() > 0)
        self.motionBox.stateChanged.connect(self._on_motion_toggle)
        g0.addWidget(self.motionBox)

        themeNote = CaptionLabel("深色为默认（色值经 WCAG 对比度验算，全部达标）；"
                                 "「跟随系统」会随 Windows 深浅色切换自动响应。密实度控制列表行高"
                                 "与间距缩放，切换后立即重排。")
        themeNote.setWordWrap(True)  # 920 窗宽下约 860px，不换行会被卡片右缘硬切（R189）
        g0.addWidget(themeNote)
        lay.addWidget(c0)
        self._sync_theme_btns()
        self._sync_density_btns()

        # 导航显示定制（3E+，Cherry Studio 式）：不重排不删除，用户自行收敛侧边栏
        cN = CardWidget()
        gN = QVBoxLayout(cN)
        gN.setContentsMargins(tokens.PAD_CARD_WIDE, tokens.PAD_CARD,
                                tokens.PAD_CARD_WIDE, tokens.PAD_CARD)
        gN.addWidget(section_head("导航显示", "MENU"))
        navNote = CaptionLabel(
            "取消勾选的页面从侧边栏收起（总览/设置固定显示）；隐藏的页面仍可用 Ctrl+K 命令面板或 Ctrl+数字 打开；重启后生效。")
        navNote.setWordWrap(True)
        gN.addWidget(navNote)
        self._navChecks = {}
        _hiddenNow = set(core.load_config().get("nav_hidden", []) or [])
        for _nm in NAV_CUSTOMIZABLE:
            _cb = CheckBox(_nm)
            _cb.setChecked(_nm not in _hiddenNow)
            self._navChecks[_nm] = _cb
            gN.addWidget(_cb)
        _saveNav = PushButton(ic("SAVE", "INFO"), "保存导航设置")
        _saveNav.clicked.connect(self._save_nav)
        gN.addWidget(_saveNav)
        lay.addWidget(cN)

        c1 = CardWidget()
        g1 = QVBoxLayout(c1)
        g1.setContentsMargins(tokens.PAD_CARD_WIDE, tokens.PAD_CARD,
                                tokens.PAD_CARD_WIDE, tokens.PAD_CARD)
        g1.addWidget(section_head("根目录", "FOLDER"))
        row = QHBoxLayout()
        self.rootLabel = BodyLabel("未设置")
        pickBtn = PushButton(ic("FOLDER_ADD", "INFO"), "选择…")
        self.initBtn = PrimaryPushButton(ic("EMBED", "INFO"), "初始化目录结构")
        row.addWidget(self.rootLabel, 1)
        row.addWidget(pickBtn)
        row.addWidget(self.initBtn)
        g1.addLayout(row)
        # R774：路径不可用/未初始化此前只在窗口横幅里说（R772），可设置页才是动手修的地方——
        # 横幅关掉后这一页仍只显示路径本身，看不出「这目录现在是坏的」。
        self._root_tone = "neutral"
        self.rootState = CaptionLabel("")
        self.rootState.setWordWrap(True)
        bind_style(self.rootState, self._paint_root_state)
        g1.addWidget(self.rootState)
        g1.addWidget(CaptionLabel("可以直接选现有目录（如桌面 zcode项目记录）立即可视化历史；"
                                 "也可以选新目录（推荐 D:\\AgentHub）后初始化全新结构。软件纯本地扫描，不改任何 agent。"))
        lay.addWidget(c1)

        c1b = CardWidget()
        g1b = QVBoxLayout(c1b)
        g1b.setContentsMargins(tokens.PAD_CARD_WIDE, tokens.PAD_CARD,
                                tokens.PAD_CARD_WIDE, tokens.PAD_CARD)
        g1b.addWidget(section_head("大脑数据库（_hub\\brain.db —— 记忆/记录/流水的唯一真理）", "SAVE"))
        dbRow = QHBoxLayout()
        self.backupBtn = PrimaryPushButton(ic("SAVE", "INFO"), "立即备份大脑")
        self.backupBtn.clicked.connect(self.backup_now)
        dbRow.addWidget(self.backupBtn)
        self.dbLabel = CaptionLabel("")
        dbRow.addWidget(self.dbLabel, 1)
        g1b.addLayout(dbRow)
        g1b.addWidget(CaptionLabel("每次退出软件自动备份（保留 30 份，在 _hub\\brain-backups\\）；"
                                   "历史 md/jsonl 已增量迁移入库并保留为只读存档。"))
        lay.addWidget(c1b)

        # 系统自检（R225）：把只存在于命令行的 `cli.py doctor` 搬进界面。
        # 为什么放设置页：它属于「维护」而不是日常浏览；帮助页原先写着"维护者用：不开界面
        # 也能自检"，等于承认界面里没有入口——用户遇到问题只能在文档和终端之间来回跳。
        c1d = CardWidget()
        g1d = QVBoxLayout(c1d)
        g1d.setContentsMargins(tokens.PAD_CARD_WIDE, tokens.PAD_CARD,
                                tokens.PAD_CARD_WIDE, tokens.PAD_CARD)
        g1d.addWidget(section_head("系统自检（一键看清哪里不对劲）", "HEART"))
        g1d.addWidget(CaptionLabel(
            "跑完一整套检查：数据库完整性 / 环境 / 备份 / 规则与采集 / 接入参数漂移 / "
            "治理积压 / 待办与停滞项目…失败项直接给可执行修复口径。等价于命令行 python cli.py doctor。"))
        patrolRow = QHBoxLayout()
        self._patrol_busy = False
        self._patrol_report = None
        self.patrolBtn = PrimaryPushButton(ic("SYNC", "INFO"), "运行自检")
        self.patrolBtn.clicked.connect(self.run_patrol_check)
        self.patrolCopyBtn = PushButton(ic("COPY", "INFO"), "复制报告")
        self.patrolCopyBtn.setEnabled(False)
        self.patrolCopyBtn.clicked.connect(self.copy_patrol)
        self.patrolState = CaptionLabel("尚未运行")
        patrolRow.addWidget(self.patrolBtn)
        patrolRow.addWidget(self.patrolCopyBtn)
        patrolRow.addWidget(self.patrolState, 1)
        g1d.addLayout(patrolRow)
        self.patrolView = TextBrowser()
        self.patrolView.setMinimumHeight(180)
        self.patrolView.setVisible(False)      # 没跑过就不占版面
        g1d.addWidget(self.patrolView)
        lay.addWidget(c1d)

        c1c = CardWidget()
        g1c = QVBoxLayout(c1c)
        g1c.setContentsMargins(tokens.PAD_CARD_WIDE, tokens.PAD_CARD,
                                tokens.PAD_CARD_WIDE, tokens.PAD_CARD)
        g1c.addWidget(section_head("窗口大小（点一下立即生效，边缘拖拽外的一键兜底）", "ZOOM"))
        sizeRow = QHBoxLayout()
        for label, wd, ht in (("紧凑 1180×760", 1180, 760), ("标准 1400×900", 1400, 900),
                              ("宽敞 1616×950", 1616, 950)):
            b = PushButton(label)
            b.clicked.connect(lambda _, w_=wd, h_=ht: self.win.showNormal() or self.win.resize(w_, h_))
            sizeRow.addWidget(b)
        sizeRow.addStretch(1)
        g1c.addLayout(sizeRow)
        g1c.addWidget(CaptionLabel("也可以用键盘：Win+←/→ 贴靠半屏，Alt+空格→大小 用方向键精调。"))
        lay.addWidget(c1c)

        c2 = CardWidget()
        g2 = QVBoxLayout(c2)
        g2.setContentsMargins(tokens.PAD_CARD_WIDE, tokens.PAD_CARD,
                                tokens.PAD_CARD_WIDE, tokens.PAD_CARD)
        g2.addWidget(section_head("规则（存 _hub\\rules.md，将来 agent 接入后由工具注入）", "DOCUMENT"))
        self.rulesEdit = TextEdit()
        self.rulesEdit.setMinimumHeight(220)
        g2.addWidget(self.rulesEdit)
        saveBtn = PushButton(ic("SAVE", "INFO"), "保存规则")
        g2.addWidget(saveBtn, 0, Qt.AlignRight)
        lay.addWidget(c2)
        lay.addStretch(1)

        pickBtn.clicked.connect(self.pick_root)
        self.initBtn.clicked.connect(self.init_struct)
        saveBtn.clicked.connect(self.save_rules)

    def _save_nav(self):
        """保存导航显隐（3E+，重启生效；隐藏页仍可用 Ctrl+K / Ctrl+数字 打开）。"""
        hidden = [nm for nm, cb in self._navChecks.items() if not cb.isChecked()]
        cfg = core.load_config()
        cfg["nav_hidden"] = hidden
        core.save_config(cfg)
        InfoBar.success("导航设置已保存",
                        "重启 AgentHub 后生效；隐藏的页面仍可用 Ctrl+K 命令面板打开",
                        duration=3500, parent=self.win)

    def _sync_density_btns(self):
        """密实度三键同步：当前档禁用（表达"已选中"）。"""
        cur = tokens.load_density_choice()
        for _k, _b in self._densBtns.items():
            _b.setEnabled(_k != cur)
            _b.setToolTip("当前密实度" if _k == cur else f"切换到{_b.text()}")

    def _on_density_pick(self, mode: str):
        """切换密实度：落配置 → 生效 → 重刷（行高与间距缩放按新档重排）。"""
        tokens.save_density_choice(mode)
        tokens.set_density(mode)
        self._sync_density_btns()
        self.win.refresh()
        InfoBar.success("密实度已切换", f"列表行高 {tokens.row_height()}px",
                        duration=2500, parent=self.win)

    def _on_motion_toggle(self, _state=None):
        """动效开关（0 = 瞬时切换，无障碍/低性能）。"""
        cfg = core.load_config()
        cfg["ui_motion"] = "on" if self.motionBox.isChecked() else "off"
        core.save_config(cfg)

    def _sync_theme_btns(self):
        """外观三键同步：当前主题键禁用（表达"已选中"），其余可点。"""
        cur = tokens.load_theme_choice()
        for _m, _b in self._themeBtns.items():
            _b.setEnabled(_m != cur)
            _b.setToolTip("当前主题" if _m == cur else f"切换到{tokens.MODE_CN[_m]}")

    def _on_theme_pick(self, mode):
        """外观切换（深/浅/跟随系统）：两段式应用（覆盖 qfw setTheme 短路）+ 持久化 + 全窗重刷。
        重刷让 HTML 内联色（tokens.C 生成）随主题更新；qfw 组件由 setTheme 自动换肤。"""
        tokens.apply_theme(mode)
        tokens.save_theme_choice(mode)
        self._sync_theme_btns()
        InfoBar.success("外观已切换", f"主题：{tokens.MODE_CN.get(mode, mode)}",
                        duration=1500, parent=self.win)
        defer(self, self.win.refresh, 80)

    def showEvent(self, e):
        self.reload()
        super().showEvent(e)

    def reload(self):
        root = self.win.root or ""
        self.reload_root_state()
        if root:
            self.rulesEdit.setPlainText(core.load_rules(root))

    def reload_root_state(self):
        """只重算根目录状态行（含路径标签）——规则编辑区不动。

        R775：F5 刷新成功后 window.apply 会调它，让用户不必切页就能看到
        「目录不可用 → 目录可用」。不直接调 reload() 是为了不覆盖正在编辑的规则文本。
        """
        root = self.win.root or ""
        self.rootLabel.setText(root or "未设置")
        if not root:
            self._root_tone = "neutral"
            self.rootState.setText(
                "还没有选择根目录——点「选择…」选一个目录；新目录可再点「初始化目录结构」")
        elif not os.path.isdir(root):
            self._root_tone = "warning"
            # R165：qfw 的 CaptionLabel 用 Segoe UI，✓/⚠ 会渲染成方框（字形门禁盯着），
            # 状态语义一律用「！」/「●」这类有字形的符号表达。
            self.rootState.setText(
                "！目录不可用：可能盘符变了或移动硬盘未连接——点「选择…」重新指到可用目录")
        else:
            self._root_tone = "success"
            self.rootState.setText(
                "● 目录可用 · 大脑已初始化"
                if os.path.isdir(os.path.join(root, core.DIR_META))
                else "● 目录可用 · 大脑尚未初始化——点「初始化目录结构」启用共享记忆与流水")
        self._paint_root_state()

    def _paint_root_state(self):
        """状态行随主题换色；bind_style 已登记，换肤时自动重算（R223 同族）。"""
        spec = {"warning": tokens.WARNING,
                "success": tokens.SUCCESS}.get(getattr(self, "_root_tone", "neutral"))
        self.rootState.setStyleSheet(f"color:{tokens.C(spec)};" if spec else "")

    def pick_root(self):
        from PySide6.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, "选择根目录", self.win.root or "")
        if d:
            self.win.set_root(d)

    def backup_now(self):
        if not self.win.root:
            InfoBar.warning("先选择根目录", "先在上面选好数据根目录，再进行此操作", duration=2000, parent=self.win)
            return
        err = brain.backup_brain(self.win.root)
        if err.startswith("_") or "失败" in err:
            InfoBar.error("备份失败", err, duration=4000, parent=self.win)
        else:
            self.dbLabel.setText(f"上次备份：{err}")
            InfoBar.success("大脑已备份", err, duration=3000, parent=self.win)

    def init_struct(self):
        if not self.win.root:
            InfoBar.warning("先选择根目录", "先在上面选好数据根目录，再进行此操作", duration=2000, parent=self.win)
            return
        # R146：低频结构创建操作加确认——误点会在选定目录里建出一套结构
        #（00_Inbox/99_Archive/_hub…），选错目录的清理成本远高于点一次确认。
        from PySide6.QtWidgets import QMessageBox
        ret = QMessageBox.question(
            self, "初始化目录结构",
            f"将在以下目录创建 AgentHub 结构（00_Inbox / 99_Archive / _hub 等，已存在的保持不变）：\n"
            f"{self.win.root}\n\n继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        err = core.init_hub(self.win.root)
        if err:
            InfoBar.error("初始化失败", err, duration=4000, parent=self.win)
            return
        # R228：库跟结构一起建——按钮叫「初始化目录结构」、提示说「已初始化」，
        # 就不该把建库留给下次载入根目录（CLI 的 init 也是这两步一起做）。
        err = brain.init_db(self.win.root)
        if err:
            InfoBar.error("大脑建库失败", err, duration=5000, parent=self.win)
            return
        InfoBar.success("已初始化", "00_Inbox / 99_Archive / _hub（规则·共享记忆·操作流水·错误登记）+ 大脑库",
                        duration=3000, parent=self.win)
        self.win.refresh()

    def save_rules(self):
        if not self.win.root:
            return
        err = core.save_rules(self.win.root, self.rulesEdit.toPlainText())
        if err:
            InfoBar.error("保存失败", err, duration=3000, parent=self.win)
        else:
            InfoBar.success("已保存", "", duration=1500, parent=self.win)

    # ---------------------------------------------------------------- 系统自检

    def run_patrol_check(self):
        """一键自检（R225）：后台跑 ops.run_patrol，结果回主线程渲染。

        必须走后台：巡检里 db_integrity 要真读一遍库、stalled/inbox 要扫目录，
        真实库可达数秒；同步跑会把窗口冻住，用户以为软件卡死——而"卡住的体检"
        恰好是用户最需要它的时候。
        """
        if self._patrol_busy:
            return
        root = self.win.root or ""
        if not root:
            InfoBar.warning("还没有设置根目录",
                            "先在上面「根目录」卡片里选一个目录，再运行自检",
                            duration=3000, parent=self.win)
            return
        self._patrol_busy = True
        self.patrolBtn.setEnabled(False)
        self.patrolState.setText("正在检查…")
        self._patrol_prog = {"done": 0, "total": 0, "name": ""}
        if getattr(self, "_patrol_timer", None) is None:
            self._patrol_timer = QTimer(self)
            self._patrol_timer.setInterval(150)
            self._patrol_timer.timeout.connect(self._patrol_tick)
        self._patrol_timer.start()
        w = FnWorker(lambda: ops.run_patrol(root, progress=self._on_patrol_progress), self)
        w.done.connect(self._on_patrol_done)
        if getattr(self, "_workers", None) is None:
            self._workers = []
        self._workers.append(w)
        w.start()

    def _on_patrol_progress(self, done, total, name):
        """后台线程回调：**只写数据不碰控件**——Qt 控件只能在主线程改，主线程侧由
        `_patrol_tick` 轮询这个字典（比跨线程信号简单，且不会漏掉最后一帧）。"""
        prog = getattr(self, "_patrol_prog", None)
        if prog is not None:
            prog["done"], prog["total"], prog["name"] = done, total, name

    def _patrol_tick(self):
        prog = getattr(self, "_patrol_prog", None) or {}
        if prog.get("total"):
            self.patrolState.setText(
                f"正在检查 {prog['done']}/{prog['total']}：{prog['name']}…")

    def _on_patrol_done(self, rep):
        self._patrol_busy = False
        self.patrolBtn.setEnabled(True)
        timer = getattr(self, "_patrol_timer", None)
        if timer is not None:
            timer.stop()
        self.patrolView.setVisible(True)
        if isinstance(rep, Exception):
            self.patrolState.setText("自检执行失败")
            self.patrolView.setPlainText(
                f"自检执行失败（体检器自身异常，不是数据问题）：\n"
                f"{type(rep).__name__}: {rep}")
            return
        self._patrol_report = rep
        self.patrolCopyBtn.setEnabled(True)
        self.patrolView.setPlainText(self._patrol_text(rep))
        self.patrolState.setText(self._patrol_headline(rep))

    @staticmethod
    def _patrol_headline(rep) -> str:
        """一行结论（口径与 cli.py doctor 保持一致，避免两处说法打架）。"""
        n_decide = rep.get("decide_count", 0)
        n_fix = len(rep.get("issues", [])) - n_decide
        if n_decide and n_fix:
            split = f"（可直接修复 {n_fix} · 需你拍板 {n_decide}）"
        elif n_decide:
            split = f"（均需你拍板 {n_decide}）"
        else:
            split = ""
        same = rep.get("same_cause_skipped", 0)
        if same:
            head = (f"{rep.get('checked', 0)}/{rep.get('total', rep.get('checked', 0))} 项"
                    f"（同因跳过 {same}）")
        else:
            head = f"{rep.get('checked', 0)} 项"
        return (f"自检 {head}，通过 {len(rep.get('passed', []))}，"
                f"问题 {len(rep.get('issues', []))}{split} · {rep.get('elapsed_ms', 0)}ms")

    @staticmethod
    def _patrol_text(rep) -> str:
        lines = ["", "通过："]
        lines += [f"  ✓ {n}" for n in rep.get("passed", [])] or ["  （无）"]
        issues = rep.get("issues", [])
        lines += ["", f"问题 {len(issues)} 项："]
        if not issues:
            lines.append("  （无）")
        for i in issues:
            tags = []
            if i.get("scope") == "machine":
                tags.append("机器级")
            if i.get("kind") == "decide":
                tags.append("要拍板")
            tag = f"（{' · '.join(tags)}）" if tags else ""
            lines.append(f"  ✗ {i.get('name', '')}{tag}：{i.get('summary', '')}")
            fix = i.get("fix_brief") or i.get("fix") or ""
            if fix:
                lines.append(f"     修复：{fix}")
            n_same = i.get("skipped_same_cause") or 0
            if n_same:
                lines.append(f"     注：另有 {n_same} 项依赖大脑库的检查因同一原因跳过"
                             f"（建库/指对根目录后自动恢复）")
            for d in (i.get("detail") or [])[:5]:
                lines.append(f"       · {d}")
        n_machine = sum(1 for i in issues if i.get("scope") == "machine")
        if n_machine:
            lines.append(f"  注：{n_machine} 项为机器级状态（运行进程 / 全局 agent 配置），"
                         f"与当前根目录无关，换根目录也会照实报。")
        skipped = rep.get("skipped", [])
        if skipped:
            lines += ["", "跳过："]
            lines += [f"  … {s}" for s in ops.format_skipped(skipped)]
        slow = sorted(rep.get("timings") or [], key=lambda x: -x.get("ms", 0))[:3]
        if slow and slow[0].get("ms", 0) >= 1000:
            lines += ["", "耗时 Top3：" + " · ".join(
                f"{t.get('name', '')} {t.get('ms', 0) / 1000:.1f}s" for t in slow)]
        return "\n".join(lines)

    def copy_patrol(self):
        rep = self._patrol_report
        if not rep:
            return
        QApplication.clipboard().setText(self._patrol_headline(rep) + self._patrol_text(rep))
        InfoBar.success("已复制自检报告", "可直接粘贴到工单/对话里", duration=2000, parent=self.win)

