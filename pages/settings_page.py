# -*- coding: utf-8 -*-
"""SettingsPage（P2-1 自 ui.py 拆出）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403
import uikit  # noqa: F401  （AGENT_CN/_AGENT_DISP2ID 等运行期重绑名请用 uikit.X 动态取）
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
        QTimer.singleShot(80, self.win.refresh)

    def showEvent(self, e):
        self.reload()
        super().showEvent(e)

    def reload(self):
        self.rootLabel.setText(self.win.root or "未设置")
        if self.win.root:
            self.rulesEdit.setPlainText(core.load_rules(self.win.root))

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
        InfoBar.success("已初始化", "00_Inbox / 99_Archive / _hub（规则·共享记忆·操作流水·错误登记）",
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

