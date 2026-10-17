# -*- coding: utf-8 -*-
"""主窗口 + 启动（P2-3 自 ui.py 拆出；三层拆分收口：uikit=共享基建 / pages=各页 / window=主窗与启动）。"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403  共享基建再导出
from uikit import __all__ as _uikit_all  # noqa: F401
import actions  # P2-2 动作注册表（导航/快捷键/命令面板同源）

# —— 页面（P2-1 拆到 pages/）——
from pages.project_page import ProjectPage  # noqa: F401
from pages.today_page import TodayPage  # noqa: F401
from pages.signal_page import SignalPage  # noqa: F401
from pages.governance_page import GovernancePage  # noqa: F401
from pages.playbook_page import PlaybookPage  # noqa: F401
from pages.timeline_page import TimelinePage, VIRTUAL_THRESHOLD  # noqa: F401
from pages.brain_page import BrainPage  # noqa: F401
from pages.notes_page import NotesPage  # noqa: F401
from pages.stats_page import StatsPage  # noqa: F401
from pages.search_page import SearchPage  # noqa: F401
from pages.audit_page import AuditPage  # noqa: F401
from pages.journal_page import JournalPage  # noqa: F401
from pages.ledger_page import LedgerPage  # noqa: F401
from pages.overview_page import OverviewPage  # noqa: F401
from pages.hub_page import HubPage  # noqa: F401
from pages.connect_page import ConnectPage  # noqa: F401
from pages.report_page import ReportPage  # noqa: F401
from pages.help_page import HelpPage  # noqa: F401
from pages.components_page import ComponentsPage  # noqa: F401
from pages.settings_page import SettingsPage  # noqa: F401


# ---------------------------------------------------------------- 主窗口

class AgentHubWindow(FluentWindow):
    """主窗口。边缘缩放：原生 hit-test（BORDER_WIDTH 加宽）+ Qt QSizeGrip 角落手柄双保险。"""

    def __init__(self):
        super().__init__()
        self.root = core.get_root()
        core.JOURNAL_SINK = brain.journal_add  # 流水落库
        # 密实度（P4 设置页开关）：必须在建页面前生效——行高与间距在建布局时就读了
        tokens.set_density(tokens.load_density_choice())
        self._workers = []
        if self.root and os.path.isdir(self.root):
            # 建表 + 历史 md/jsonl 增量迁移（幂等），后台跑防卡启动
            w = FnWorker(lambda: brain.init_db(self.root), self)
            w.done.connect(self._on_db_ready)
            self._workers.append(w)
            w.start()

        self.overview_page = OverviewPage(self)
        self.today_page = TodayPage(self)
        self.signal_page = SignalPage(self)
        self.governance_page = GovernancePage(self)
        self.playbook_page = PlaybookPage(self)
        self.project_page = ProjectPage(self)
        self.timeline_page = TimelinePage(self)
        self.stats_page = StatsPage(self)
        self.brain_page = BrainPage(self)
        self.notes_page = NotesPage(self)
        self.report_page = ReportPage(self)
        self.search_page = SearchPage(self)
        self.ledger_page = LedgerPage(self)
        self.audit_page = self.ledger_page.audit      # 原对账页别名，apply() 链路不变
        self.journal_page = self.ledger_page.journal  # 原流水页别名，reload 链路不变
        self.hub_page = HubPage(self)
        self.connect_page = ConnectPage(self)
        self.help_page = HelpPage(self)
        self.components_page = ComponentsPage(self)
        self.settings_page = SettingsPage(self)

        # Fluent 导航规范：主区只放"地方"，按语义分组（工作区/数据），配置与帮助类沉底，
        # 避免 11 项平铺扫视困难（2026-10-01 参照官方示例与 MS NavigationView 指南重整）
        # 3E+ 导航定制（Cherry Studio 式）：config.nav_hidden 里的页面不入导航，但仍进
        # stackedWidget——Ctrl+K 命令面板 / Ctrl+数字 依然可达（启动台语义）。
        _hidden_at_boot = set(core.load_config().get("nav_hidden", []) or [])

        def _add(page, icon, text, pos=None):
            page.setObjectName(text)
            # 页面底色唯一入口：所有页面（含隐藏页、含将来新增页）必经此处
            # （2026-10-15 P0：原为 16 页手写名单，漏了后加的 4 页 → 深色主题白底不可见）
            apply_page_background(page)
            if text in _hidden_at_boot:
                self.stackedWidget.addWidget(page)
                return
            if pos is None:
                self.addSubInterface(page, ic(icon), text)
            else:
                self.addSubInterface(page, ic(icon), text, pos)

        # 导航（P2-2 同源）：顺序/图标/标题/分组全部来自 actions.PAGES——命令面板与快捷键读同一张表
        for _attr, _title, _icon, _group, _kw, _ctrl in actions.PAGES:
            if _group == "top":
                _add(getattr(self, _attr), _icon, _title)
        for _gkey, _header in actions.GROUP_HEADERS:
            self.navigationInterface.addItemHeader(_header)
            for _attr, _title, _icon, _group, _kw, _ctrl in actions.PAGES:
                if _group == _gkey:
                    _add(getattr(self, _attr), _icon, _title)
        for _attr, _title, _icon, _group, _kw, _ctrl in actions.PAGES:
            if _group == "bottom":
                _add(getattr(self, _attr), _icon, _title, NavigationItemPosition.BOTTOM)

        self._narrow_nav()
        # 内容区不透明底色（防"浅色主题透深色壁纸 Mica"错配，2026-10-10 实测像素级定位）：
        # 信号挂 themeChangedFinished——设置页切换/系统联动/启动，任何主题变化自动更新
        qconfig.themeChangedFinished.connect(self._apply_page_bgs)
        self._apply_page_bgs()
        # token 绑定样式（徽章/胶囊/层级卡片）：凡是创建时"现算色值"的 QSS 都不会
        # 自己跟着主题变——换肤后会出现"徽章还是旧主题色"的错配。统一登记、统一重刷。
        qconfig.themeChangedFinished.connect(refresh_token_styles)
        # 主色与主题解耦（第二轮 P0）：qfw 控件不跟 Windows 系统 accent 跑，切主题时重喂一次
        qconfig.themeChangedFinished.connect(tokens.apply_accent)
        tokens.apply_accent()
        # 边缘拖拽：qframelesswindow 原生链命中带默认 5 物理像素（高分屏拖不到），加宽兜底；
        # 另加 Qt QSizeGrip 角落手柄（startSystemResize，不依赖原生 hit-test）
        self.setResizeEnabled(True)
        self.BORDER_WIDTH = 14
        self.setMinimumSize(920, 600)
        self._grips = [QSizeGrip(self), QSizeGrip(self)]
        for g in self._grips:
            g.setFixedSize(24, 24)
            g.setStyleSheet("background: transparent;")
            g.raise_()
        self.setWindowTitle("AgentHub")
        # 窗口图标（运行中任务栏+窗口左上角）：打包时经 --add-data 带入 _MEIPASS，源码运行用仓库 assets
        _icon = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "agenthub.ico")
        if os.path.isfile(_icon):
            self.setWindowIcon(QIcon(_icon))
        self.resize(1180, 760)
        geo = core.load_config().get("win_geometry", "")
        if geo:
            from PySide6.QtCore import QByteArray
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        f5 = QShortcut(QKeySequence("F5"), self)
        f5.activated.connect(self.refresh)
        cf = QShortcut(QKeySequence("Ctrl+F"), self)
        cf.activated.connect(self.goto_search)
        # 命令面板（Ctrl+K）+ 页面直达（Ctrl+1..9，序号来自 actions.PAGES）——批次 3E 快捷键体系
        self._palette = palette.CommandPalette(self)
        ck = QShortcut(QKeySequence("Ctrl+K"), self)
        ck.activated.connect(self._palette.popup)
        for _attr, _title, _icon, _group, _kw, _ctrl in actions.PAGES:
            if _ctrl:
                _sc = QShortcut(QKeySequence(f"Ctrl+{_ctrl}"), self)
                _sc.activated.connect(lambda a=_attr: self.switchTo(getattr(self, a)))
        self._workers = getattr(self, "_workers", [])  # 迁移线程已在 __init__ 挂入
        QTimer.singleShot(200, self.refresh)
        # 开机自检（大脑自己跑，agent 不参与）：启动 5 分钟后后台跑一次 env 环境预检落流水。
        # QTimer 挂 self 为 parent——窗口销毁时定时器随之销毁，不会在死对象上触发
        self._auto_env_timer = QTimer(self)
        self._auto_env_timer.setSingleShot(True)
        self._auto_env_timer.timeout.connect(self._auto_env_check)
        self._auto_env_timer.start(5 * 60 * 1000)
        if not self.root:
            self.switchTo(self.settings_page)

    def _collect_worker_lists(self):
        """汇总窗口 + 各页面在飞的后台线程列表（退出前统一等待；页面自持 _workers）。"""
        lists = [self._workers]
        for _attr, *_rest in actions.PAGES:
            _pg = getattr(self, _attr, None)
            _lst = getattr(_pg, "_workers", None)
            if _lst:
                lists.append(_lst)
        return lists

    def _auto_env_check(self):
        """启动自检：后台跑一次 env 环境预检（run_check 内部落流水），结果静默不弹 UI——
        环境快照定期入库，异常时 agent 下次跑 env 自然看到。"""
        if not self.root:
            return
        w = FnWorker(lambda: ops.run_check("env", root=self.root), self)
        w.done.connect(lambda _r: None)
        self._workers.append(w)
        w.start()

    def _on_db_ready(self, result):
        """大脑库就绪（含历史迁移结果）。"""
        if isinstance(result, Exception):
            InfoBar.error("大脑数据库初始化失败", str(result), duration=6000, parent=self)
            return
        if result:
            InfoBar.warning("大脑建库告警", str(result), duration=6000, parent=self)
        self.refresh()  # 迁移完成后重刷，各页补上 DB 数据

    def resizeEvent(self, e):
        if getattr(self, "_grips", None):
            self._grips[0].move(self.width() - 24, self.height() - 24)  # 右下
            self._grips[1].move(0, self.height() - 24)                  # 左下
        super().resizeEvent(e)

    def _apply_page_bgs(self):
        """主题变化时重刷内容页底色（tokens bg_page：浅 #f9f9fb / 深 #161616）。

        根因（2026-10-10 多路抓图+注册表定位）：用户 Windows 为深色（AppsUseLightTheme=0），
        窗口底由 AcrylicWindow 按**系统深色**画 (30,30,30)；页面若透明，应用切浅色主题时
        会"深底浅字"错配（标题栏浅/内容区深）。修复=页面自己画不透明底，不再依赖窗口底。

        2026-10-15 P0 复盘：原实现是**手写 16 页名单**，后加的工作台/信号中心/治理中心/
        编排中心 4 页没进名单 → 深色主题下这 4 页渲染成白底（像素采样近白 79–82%，
        正常页 3–7%），内容近乎不可见。现在首次上色在 `_add()`（唯一入口）完成，
        本方法改为遍历 `actions.PAGES` 重刷——**名单消失，新增页面自动覆盖**。
        经 qconfig.themeChangedFinished 信号随任何主题变化自动更新。"""
        for _attr, _title, _icon, _group, _kw, _ctrl in actions.PAGES:
            page = actions.page_of(self, _attr)
            if page is not None:
                apply_page_background(page)

    def _narrow_nav(self):
        """导航栏按上次退出时的显示模式恢复（qfw 有 COMPACT/MENU/EXPAND 三态，
        用户点过汉堡展开后即使悬浮展开也应恢复为展开）。"""
        nav = self.navigationInterface
        try:
            nav.setCollapsible(True)
        except Exception:
            pass
        try:
            nav.setExpandWidth(170)  # v2.1 导航更名后（Agent 中心/流水·对账）96px 截断文字
        except Exception:
            pass
        panel = getattr(nav, "panel", None)
        if panel is None or not hasattr(panel, "collapse"):
            return
        if core.load_config().get("nav_collapsed", True):
            QTimer.singleShot(0, panel.collapse)  # 窗口显示后执行，初始化前调用无效
        else:
            QTimer.singleShot(0, panel.expand)

    def _nav_collapsed_now(self):
        """用户视角的收起状态：仅 COMPACT 算收起；MENU（悬浮展开）/EXPAND 都算展开。"""
        panel = getattr(self.navigationInterface, "panel", None)
        mode = getattr(panel, "displayMode", None)
        return getattr(mode, "name", "COMPACT") == "COMPACT"

    def closeEvent(self, e):
        """退出前：断后台线程信号（防 wait 超时后回调触碰已关闭的 UI）、等线程结束，备份大脑，再记住状态。
        wait 2s 超时的线程：切断 Qt 父子链并由 _DETACHED_THREADS 持引用——窗口销毁不再删除
        运行中线程（destroyed-while-running abort 即"有时候退出会报错"的根因），进程随后
        经 os._exit 硬杀，不触发析构。SQLite 写入中途被杀由 WAL/退出自动备份兜底。"""
        for lst in self._collect_worker_lists():
            for t in list(lst):
                try:
                    t.blockSignals(True)  # 阻断 done/finished 全部信号：wait 超时后线程回调不再进已关闭的界面
                    if not t.wait(2000):
                        t.setParent(None)
                        _DETACHED_THREADS.append(t)
                except Exception:
                    pass
        if self.root and os.path.isdir(self.root):
            try:
                brain.backup_brain(self.root)  # 退出自动备份大脑（轮转保留30份）
            except Exception:
                pass
        try:
            cfg = core.load_config()
            cfg["win_geometry"] = bytes(self.saveGeometry().toBase64()).decode()
            cfg["nav_collapsed"] = self._nav_collapsed_now()
            core.save_config(cfg)
        except Exception:
            pass
        super().closeEvent(e)
        # 退出挂起兜底（qfw item view+QSS 偶发挂起，os._exit 兜底须 exec 返回才生效）：
        # 走到这里备份/保存已全部完成，若 2 秒后事件循环仍未退出就硬杀，不给用户留"关不掉"。
        # 旧写法 lambda 引用 self.isVisible()——2 秒后窗口 C++ 对象可能已删（销毁时序不定），
        # 触发 "Internal C++ object already deleted" RuntimeError 未捕获 → windowed exe 弹
        # PyInstaller 错误框（退出间歇性报错的候选根因，用户报障后仍复现）。改防御式访问。
        def _force_exit():
            try:
                if self.isVisible():
                    return  # 窗口仍可见（极罕见：关闭被外部阻止）——不杀，让用户继续用
            except RuntimeError:
                pass  # C++ 对象已删——窗口确实没了，正是要硬杀的场景
            _hard_exit(0)
        QTimer.singleShot(2000, _force_exit)

    def goto_search(self):
        self.switchTo(self.search_page)
        self.search_page.edit.setFocus()

    def set_root(self, d):
        self.root = d
        core.set_root(d)
        core.init_hub(d)
        err = brain.init_db(d)
        if err:
            InfoBar.error("大脑建库失败", err, duration=5000, parent=self)
        self.settings_page.reload()
        self.refresh()

    def refresh(self):
        if not self.root:
            return
        w = ScanWorker(self.root, self)
        w.done.connect(self.apply)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def apply(self, snap):
        if snap.error and not snap.projects:
            InfoBar.error("扫描失败", snap.error, duration=4000, parent=self)
        if self.root and not (Path(self.root) / core.DIR_META).is_dir() \
                and not getattr(self, "_hub_warned", False):
            self._hub_warned = True
            InfoBar.warning("公用大脑未初始化",
                            "到「设置」页点「初始化目录结构」——启用共享记忆、操作流水、错误登记",
                            duration=6000, parent=self)
        # 文件索引刷进大脑（供全脑检索按文件名命中）
        rows = []
        for p in snap.projects:
            for group, rp, fp in p.files:
                try:
                    st = os.stat(fp)
                    rows.append((p.name, fp, Path(fp).name, group, st.st_size, st.st_mtime))
                except OSError:
                    continue
        try:
            brain.update_files_index(self.root, rows)
        except Exception:
            pass
        self.project_page.set_snapshot(snap)
        try:
            # P2-3a：>阈值行进虚拟滚动（按页后台拉取），小库走旧全量路径
            if brain.records_count(self.root) > VIRTUAL_THRESHOLD:
                self.timeline_page.set_virtual(self.root)
            else:
                self.timeline_page.set_records(brain.list_records(self.root, limit=2000))
            self.stats_page.set_db(brain.stats(self.root))
            self.brain_page.reload()
            self.today_page.reload()
            self.signal_page.reload()
            self.governance_page.reload()
            self.playbook_page.reload()
        except Exception:
            pass
        self.audit_page.set_snapshot(snap)
        self.overview_page.set_snapshot(snap)
        try:
            self.overview_page.set_db(brain.stats(self.root), brain.list_records(self.root, limit=8))
            # 展示窗口 24h（在线判定仍是缺省 30 分钟 TTL；2026-10-11：短窗口下该面板常显空）
            self.overview_page.set_sessions(brain.active_sessions(self.root, ttl=24 * 3600))
        except Exception:
            self.overview_page.set_sessions([])
        self.journal_page.reload()
        self.report_page.reload_reports()  # agent 侧 hub_report 可能生成了新报告，F5 后列表跟上
        # 防横向溢出守卫（批次 3 修复）：刷新后对全页统一巡检（幂等、无视觉副作用）
        for _pg in (self.overview_page, self.today_page, self.signal_page,
                    self.governance_page, self.playbook_page, self.project_page, self.timeline_page,
                    self.hub_page, self.notes_page, self.brain_page,
                    self.ledger_page, self.stats_page, self.report_page,
                    self.search_page, self.connect_page, self.help_page,
                    self.components_page, self.settings_page):
            guard_overflow(_pg)
            # P2 巡检式收口：列表行规格（行高/hover/分隔线）与卡片层级（e1 描边）
            # ——新增页面自动获得，不需要记得手工调 polish_*
            polish_lists(_pg)
            polish_cards(_pg)


def _install_crash_hook():
    """windowed exe 无控制台，未捕获异常只弹 PyInstaller 错误框、traceback 蒸发——
    主线程/子线程异常统一落盘 %TEMP%，退出报错可回查（2026-10-07 用户再报退出报错取证用）。
    faulthandler 兜 C 层硬崩（段错误不走 excepthook，截图脚本实测间歇性复现）。"""
    import faulthandler
    import traceback
    import tempfile
    import threading

    try:
        p = Path(tempfile.gettempdir()) / "AgentHub-fault.log"
        p.write_text("", encoding="utf-8")
        faulthandler.enable(open(p, "a", encoding="utf-8"))  # noqa: SIM115 段错误时转储原生栈
    except Exception:
        pass

    def _dump(tag, tp, val, tb):
        try:
            p = Path(tempfile.gettempdir()) / f"AgentHub-{tag}-{datetime.now():%Y%m%d-%H%M%S}.log"
            p.write_text("".join(traceback.format_exception(tp, val, tb)), encoding="utf-8")
        except Exception:
            pass

    def _hook(tp, val, tb):
        _dump("crash", tp, val, tb)
        sys.__excepthook__(tp, val, tb)

    def _thook(args):
        _dump("thread-crash", args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _hook
    threading.excepthook = _thook


def _register_note_hotkey(w):
    """注册全局热键 Ctrl+Alt+N 呼出随记捕捉小窗（Thino Global Capture 式）。
    冲突时仅提示不崩——随记页/首页输入条仍可用。返回注销函数。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, _MOD_CONTROL | _MOD_ALT, _VK_N):
            InfoBar.warning("全局热键未注册", "Ctrl+Alt+N 已被其他程序占用，"
                            "随记页与首页输入条仍可正常使用", duration=6000, parent=w)
            return None
    except Exception:
        return None
    f = _HotkeyFilter(lambda: QuickNotePopup.popup(w))
    QApplication.instance().installNativeEventFilter(f)

    def _unregister():
        try:
            ctypes.windll.user32.UnregisterHotKey(None, 1)
        except Exception:
            pass
        QApplication.instance().removeNativeEventFilter(f)

    return _unregister


def _hard_exit(code: int = 0):
    """强制退出兜底（qfw item view 已知退出挂起坑 + detached 线程段错误）：先 flush、
    关窗，再 TerminateProcess 硬杀——跳过全部 DLL 卸载回调与线程终止时的 Qt TLS
    （QThreadStorage）清理，后者在半死线程上下文偶发 access violation（faulthandler
    实证：smoke 6 跑 3 中、layout_stress 8 连跑 2 中），TerminateProcess 实测 10+
    次无段错误。硬退不走 atexit/缓冲区 flush，重定向到文件时崩溃前的 print 全丢在
    缓冲区（smoke_ui 2026-10-12 实测教训），故先 flush。非 win32 或调用失败回退
    os._exit。run_gui 收尾、tools/ 脚本、smoke_ui、layout_stress 都靠它。

    历史坑（2026-10-17 R5 揪出）：本文件曾有两个 _hard_exit——本 TerminateProcess
    版被下方一个裸 os._exit 版静默遮蔽（后定义胜），「文档化的修复」沦为死代码，
    全部调用方实际用的是弱版。已合并为本定义；adv_agenthub 退出守卫（仓库内禁裸
    os._exit，window.py 定义处除外）防复发。"""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        QApplication.instance().closeAllWindows()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.TerminateProcess(ctypes.c_void_p(k32.GetCurrentProcess()), code)
        except Exception:
            pass
    os._exit(code)


def _setup_app_font(app):
    """高分屏字体修复（2026-10-09，用户报「接入页字看不清」）。

    症状：125% 缩放屏上，缩小窗口时字形被裁掉一部分，拉大窗口才完整。
    根因：Qt 默认对缩放因子取整（Round 策略）+ DirectWrite 全 hinting，
    非整数缩放（1.25）下字形位图被舍入裁剪。
    修法：① 缩放策略改 PassThrough，保留 1.25 小数不取整；
          ② hinting 改 PreferVerticalHinting，只锁垂直方向不裁字宽；
          ③ 关掉 kerning/ligature 的位图缓存路径，走线性缩放。
    """
    # ① 高分屏缩放策略：必须在 QApplication 创建前设置
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    # ⓪ 字族统一（视觉打磨 P3 / 2026-10-15）：qfw 默认 ['Segoe UI','Microsoft YaHei',
    # 'PingFang SC'] → 拉丁字走 Segoe UI、汉字逐字回落到微软雅黑，同一行两种字体两种基线，
    # 中英混排时字重观感不齐。统一成"中文优先"的一族：拉丁字形也由雅黑 UI 提供。
    # 必须在任何控件创建前调用（qfw 在 getFont() 时读该配置）。
    try:
        from qfluentwidgets.common.font import setFontFamilies
        setFontFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"], save=False)
    except Exception:  # noqa: BLE001 取不到接口就沿用 qfw 默认，不影响启动
        pass

    f = QFont("Microsoft YaHei UI", 9)
    # ② 只做垂直 hinting：字宽不锁死，避免小字号下左右笔画被裁
    f.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    f.setKerning(True)
    app.setFont(f)
    return f


_THEME_LISTENER = None  # 系统主题监听（auto 模式联动用；进程级保引用，防 GC 回收 QThread）


def run_gui():
    global _THEME_LISTENER
    _install_crash_hook()
    tokens.apply_theme(tokens.load_theme_choice())  # 主题：两段式应用（覆盖 qfw setTheme 短路）
    # 高分屏缩放策略要在 QApplication 之前设，故在此先调一次
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    _setup_app_font(app)
    w = AgentHubWindow()
    w.show()
    _THEME_LISTENER = tokens.start_system_listener()  # 系统深浅切换联动（仅 auto 模式响应）
    _unreg = _register_note_hotkey(w)
    if _unreg:
        app.aboutToQuit.connect(_unreg)
    rc = app.exec()
    _hard_exit(rc)  # qfw item view 退出挂起兜底（已知坑）；_hard_exit 另消 detached 线程段错误


if __name__ == "__main__":
    run_gui()
