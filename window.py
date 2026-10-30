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


def _close_hooks_quiet(env, skip_flag) -> bool:
    """这个窗口实例退出时是否**什么都不该写**（不备份大脑、不存配置、不排硬退定时器）。

    判据是「它不是用户正在用的桌面会话」，不是「调用方记不记得打标记」：
    R232 只挡了 pytest（`PYTEST_CURRENT_TEST`），不带 pytest 的离屏诊断脚本照样把
    测试窗口几何写进用户真实配置（R240 实测：`output/_r240_subcost.py` 一次
    `win.close()` 覆盖了 `win_geometry`，旧值无备份不可恢复）。离屏平台根本没有
    真实桌面窗口，它的几何/导航态对用户配置就没有意义——从结构上杜绝，比要求
    每个脚本自觉可靠。

    只有 offscreen 这一档算非桌面会话：minimal/windows 等仍按生产走，否则用户
    下次开应用窗口位置每次都回默认，是比上述事故更常被察觉的回归。
    """
    if skip_flag:
        return True
    if env.get("PYTEST_CURRENT_TEST"):
        return True
    return env.get("QT_QPA_PLATFORM") == "offscreen"


# ---------------------------------------------------------------- 主窗口

class AgentHubWindow(FluentWindow):
    """主窗口。边缘缩放：原生 hit-test（BORDER_WIDTH 加宽）+ Qt QSizeGrip 角落手柄双保险。"""

    # R217：这些页的数据刷新在 apply() 里同步跑会卡住主线程（真实库实测：
    # brain 659ms + governance 617ms + stats 536ms，合计 2.36s）。它们此刻并不可见，
    # 改为「标脏 + 首次切到该页再加载」——启动只刷新当前可见页，点开某页时再付它的加载成本。
    _DB_PAGES = ("timeline_page", "stats_page", "brain_page", "today_page",
                 "signal_page", "governance_page", "playbook_page")

    # R239：这些页把取数交给后台线程。判据是实测的取数/绘制比例（output/_r239_dupes.txt）：
    # 统计页 383ms 墙钟里取数 345ms、画控件 38ms；大脑页 375/13；治理页 489/3。
    # （R243 后大脑页取数只剩 ~40ms——只取屏幕上真会画的东西，见 output/_r243_brain_cost.txt。）
    # 也就是说 R217 的「点开这页时再付它的成本」加上 R238 的「付完再上屏」合起来
    # = 切页硬冻 0.4s（期间不重绘、不响应点击）。既然成本几乎全在数据库而不在绘制，
    # 就把取数挪出 UI 线程、上屏时先盖骨架屏，回到主线程只干那 3~38ms 的渲染。
    # 前提：页面实现 fetch_db(root)（只读库）+ apply_db(bundle)（只碰控件）——
    # timeline/search/report/settings/hub 早就按 FnWorker 这套惯例走，不是新发明。
    _DB_ASYNC = ("stats_page", "brain_page")

    def __init__(self):
        super().__init__()
        self.root = core.get_root()
        core.JOURNAL_SINK = brain.journal_add  # 流水落库
        # 密实度（P4 设置页开关）：必须在建页面前生效——行高与间距在建布局时就读了
        tokens.set_density(tokens.load_density_choice())
        self._workers = []
        self._apply_count = 0  # 首次全局刷新的完成次数（走查工具据此等启动完成）
        # 懒刷新的脏名单**从构造起就得在**：原先只在 apply() 里创建，于是首轮扫描落地之前
        # （真实库实测 887ms）切到数据页时 switchTo 走 hasattr 为假的分支——既不加脏也不
        # 加载，页面空白且没人负责补（R238）。
        self._db_dirty = set(self._DB_PAGES)
        # 异步取数的在飞账本：_db_seq[attr]＝该页最后一次请求的序号，回调结果序号不符
        # 就直接丢（刷新后到达的旧读取不许盖掉新数据）；_db_pending 防连点同一页叠线程。
        self._db_seq = {}
        self._db_pending = set()
        self._db_req = 0
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
        defer(self, self.refresh, 200)   # R239：延后回调挂窗口寿命，拆窗后不再在死控件上刷新
        # 开机自检（大脑自己跑，agent 不参与）：启动 5 分钟后后台跑一次 env 环境预检落流水。
        # QTimer 挂 self 为 parent——窗口销毁时定时器随之销毁，不会在死对象上触发
        self._auto_env_timer = QTimer(self)
        self._auto_env_timer.setSingleShot(True)
        self._auto_env_timer.timeout.connect(self._auto_env_check)
        self._auto_env_timer.start(5 * 60 * 1000)
        if not self.root:
            self.switchTo(self.settings_page)
        elif not os.path.isdir(self.root):
            # R772：盘符变了/移动硬盘没插时，此前既不建库也不扫描——总览页显示「项目总数 0」，
            # 看起来就是一个空库（同 R762「读失败被当成空库」）。给一条持久的病因+下一步，
            # 用户关掉横幅后仍可按 F5 重试（R766 的扫描失败提示走的是那条路）。
            # defer 而非 QTimer.singleShot：延后回调必须挂窗口寿命（R239 门禁）。
            defer(self, self._warn_dead_root)

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
        经 qconfig.themeChangedFinished 信号随任何主题变化自动更新。

        R224：只对**当前可见页**立刻重刷，其余只标脏（`invalidate_page_background`），
        等 `switchTo` 显示前补齐。理由：给页面根 setStyleSheet 会触发整页 re-polish
        （实测约 17ms/页 × 18 页 ≈ 290ms），而换肤那一刻 17/18 的页面并不可见——
        这是"换肤 787ms"里最后一块可省的钱。标脏不清 QSS：未补刷的页面仍带上一版
        不透明底色，不会退化成透明页（R194 白底不可见那类 P0 的防线不靠"立刻全刷"）。
        """
        cur = self.stackedWidget.currentWidget()
        for _attr, _title, _icon, _group, _kw, _ctrl in actions.PAGES:
            page = actions.page_of(self, _attr)
            if page is None:
                continue
            if page is cur:
                apply_page_background(page)
            else:
                invalidate_page_background(page)

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
            defer(panel, panel.collapse)  # 窗口显示后执行，初始化前调用无效（定时器随面板销毁）
        else:
            defer(panel, panel.expand)

    def _nav_collapsed_now(self):
        """用户视角的收起状态：仅 COMPACT 算收起；MENU（悬浮展开）/EXPAND 都算展开。"""
        panel = getattr(self.navigationInterface, "panel", None)
        mode = getattr(panel, "displayMode", None)
        return getattr(mode, "name", "COMPACT") == "COMPACT"

    def closeEvent(self, e):
        """退出前：断后台线程信号（防 wait 超时后回调触碰已关闭的 UI）、等线程结束，备份大脑，再记住状态。
        wait 2s 超时的线程：切断 Qt 父子链并由 _DETACHED_THREADS 持引用——窗口销毁不再删除
        运行中线程（destroyed-while-running abort 即"有时候退出会报错"的根因），进程随后
        经 os._exit 硬杀，不触发析构。SQLite 写入中途被杀由 WAL/退出自动备份兜底。

        _skip_close_hooks=True 供测试收尾：断线程信号后直接返回，不备份、不存配置、不排
        2s 硬退定时器——定时器一旦触发 os._exit(0) 会连同 pytest 整个进程一起杀掉，
        表现为套件跑完一个用例后静默退出、无汇总。

        R232：pytest 进程内默认静默——测试/诊断工具常忘了设 _skip_close_hooks
        （gui_snapshot 就漏了），一次 w.close() 把 920×600 的测试窗口几何写进用户
        真实配置，下次开应用窗口尺寸就被改掉。PYTEST_CURRENT_TEST 由 pytest 注入且
        会被子进程继承，作为兜底闸门；生产运行不受影响。

        R240：判据抽成 `_close_hooks_quiet`，并把**离屏平台**也算非桌面会话——
        R232 那条只挡 pytest，本轮诊断脚本（不经 pytest 的 offscreen 探针）又写了
        一次用户配置。见该函数 docstring。"""
        quiet = _close_hooks_quiet(os.environ, getattr(self, "_skip_close_hooks", False))
        for lst in self._collect_worker_lists():
            for t in list(lst):
                try:
                    t.blockSignals(True)  # 阻断 done/finished 全部信号：wait 超时后线程回调不再进已关闭的界面
                    if not t.wait(250 if quiet else 2000):
                        t.setParent(None)
                        _DETACHED_THREADS.append(t)
                except Exception:
                    pass
        if not quiet:
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
        if quiet:
            return
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

    def _warn_dead_root(self):
        """R772：持久横幅（duration<0 不自动淡出），关掉后靠 F5 仍能复现失败提示。"""
        try:
            self._root_bar = InfoBar.error(
                "根目录不存在",
                f"{self.root} —— 可能盘符变了或移动硬盘未连接；到「设置」页重新选择根目录",
                duration=-1, parent=self)
        except Exception:  # noqa: BLE001  UI 提示失败不许影响启动
            self._root_bar = None

    def set_root(self, d):
        # R296：先写配置再改内存——core.set_root 会因 Temp 路径/权限抛错，
        # 之前先赋 self.root，异常后界面指向新目录、配置却还是旧的。
        try:
            core.set_root(d)
        except (ValueError, OSError) as e:
            InfoBar.error("根目录设置失败", str(e), duration=6000, parent=self)
            return
        self.root = d
        bar = getattr(self, "_root_bar", None)
        if bar is not None:
            try:
                bar.close()   # R772：换到可用根目录后，别再挂着「根目录不存在」
            except RuntimeError:
                pass
            self._root_bar = None
        err = core.init_hub(d)
        if err:
            InfoBar.error("目录结构初始化失败", err, duration=6000, parent=self)
        err = brain.init_db(d)
        if err:
            InfoBar.error("大脑建库失败", err, duration=5000, parent=self)
        self.settings_page.reload()
        self.refresh()

    def switchTo(self, page, *args, **kwargs):  # noqa: N802  qfluentwidgets 同名 API
        """切页即兑现懒刷新：apply() 只把数据页标脏，真正加载发生在用户要看它时。

        R224：页面底色同理——换肤只刷可见页，其余标脏；这里在 show **之前**补齐，
        避免"先白闪一帧再变成目标主题色"。已是当前色时是空操作（不多付 re-polish）。

        R238：数据也一并提到 show **之前**（底色与数据同一条口径），并且只有真填上
        才消脏；填不上就保留标记。启动窗口里首轮扫描尚未落地时，脏名单从构造起就在，
        切页因此不会"既不加载也不留标记"。

        R239 修订：`_DB_ASYNC` 里的页做不到"上屏前填好"——那 345ms 取数压在 UI 线程
        就是切页硬冻。改为上屏前盖**骨架屏**（真忙态），不变量从「上屏前填好数据」
        收紧回「上屏时页面上必须有东西：数据或忙态，绝不是一屏 0/空白」，脏标记
        仍只在数据真正落地后消。
        """
        apply_page_background(page)
        if hasattr(self, "_db_dirty"):
            attr = self._page_attr(page)
            if attr in self._db_dirty:
                # 先填数据**再**让页面上屏：super().switchTo 之后才加载的话，用户看到的
                # 是一帧空白再蹦出内容（实测治理页 509ms、大脑页 402ms、统计页 354ms）。
                # 也只有真填上才消脏——失败/没选目录时保留标记，下次切页或下次刷新再试，
                # 消掉就等于让这页空白到下一次全局刷新。
                if self._reload_db_page(attr):
                    self._db_dirty.discard(attr)
        super().switchTo(page, *args, **kwargs)

    def _page_attr(self, page) -> str:
        for attr, *_ in actions.PAGES:
            if getattr(self, attr, None) is page:
                return attr
        return ""

    def _reload_db_page(self, attr: str, force: bool = False) -> bool:
        """按需加载一个数据页；返回是否真的填上了（False＝保持脏）。"""
        if not self.root:
            return False  # 还没选目录：读库只会得到"没有数据"的假状态，不如不填
        if attr in self._DB_ASYNC:
            page = getattr(self, attr, None)
            if page is not None and hasattr(page, "fetch_db"):
                return self._start_db_fetch(attr, page, force)
            # 页面还没做取数/渲染分离：退回同步加载，语义与 R238 一致
        try:
            if attr == "timeline_page":
                if brain.records_count(self.root) > VIRTUAL_THRESHOLD:
                    self.timeline_page.set_virtual(self.root)
                else:
                    self.timeline_page.set_records(brain.list_records(self.root, limit=2000))
            elif attr == "stats_page":
                self.stats_page.set_db(brain.stats(self.root))
            else:
                getattr(self, attr).reload()
        except Exception as e:  # noqa: BLE001 单页加载失败不该拦住切页
            # R762：失败必须让用户看见。原来只在 except 里 return False，页面继续显示
            # 上一次的内容——用户以为看到的是新数据。异步页有自己的失败占位（R239），
            # 同步页统一走窗口级提示，并去重（同一页连续失败不刷屏）。
            self._warn_db_failure(attr, e)
            return False
        if hasattr(self, "_db_failed"):
            self._db_failed.discard(attr)
        return True

    def _warn_db_failure(self, attr: str, err: Exception) -> None:
        """数据页读失败的一次性提示（成功重读后自动解除去重）。"""
        if not hasattr(self, "_db_failed"):
            self._db_failed = set()
        if attr in self._db_failed:
            return
        self._db_failed.add(attr)
        title = next((t for a, t, *_ in actions.PAGES if a == attr), attr)
        InfoBar.error("数据加载失败",
                      f"「{title}」读取失败：{err}（下次切页或按 F5 重试；其余页面不受影响）",
                      duration=6000, parent=self)

    def _start_db_fetch(self, attr: str, page, force: bool) -> bool:
        """取数交给 FnWorker（后台线程），回来只渲染；返回 False＝消脏责任在回调。"""
        if attr in self._db_pending and not force:
            return False  # 该页已有一次读取在飞：连点不该再叠一条线程
        self._db_req += 1
        seq = self._db_req
        self._db_seq[attr] = seq
        self._db_pending.add(attr)
        root = self.root
        try:
            page.show_busy()
        except Exception:  # noqa: BLE001 占位画不出来不影响取数
            pass
        w = FnWorker(lambda: page.fetch_db(root), self)
        w.done.connect(lambda res, a=attr, s=seq: self._on_db_fetched(a, s, res))
        w.finished.connect(lambda wk=w: self._workers.remove(wk) if wk in self._workers else None)
        self._workers.append(w)
        w.start()
        return False

    def _on_db_fetched(self, attr: str, seq: int, res) -> None:
        """取数回来了（已在主线程）：过期结果直接丢，其余按成功/失败各自收口。"""
        if self._db_seq.get(attr) != seq:
            return  # 刷新/重新请求之后才到达的旧读取——渲染它就会用旧数据盖新数据
        self._db_pending.discard(attr)
        page = getattr(self, attr, None)
        if page is None:
            return
        if isinstance(res, Exception):
            try:
                page.show_failure(res)
            except Exception:  # noqa: BLE001 报错文案本身画不出来不该再炸一次
                pass
            return  # 失败保持标脏：下次切页/刷新还会重试，不静默留白
        try:
            page.apply_db(res)
        except Exception as e:  # noqa: BLE001
            try:
                page.show_failure(e)
            except Exception:
                pass
            return
        self._db_dirty.discard(attr)

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
            # R766：同一根目录下刷新失败，保留上一份内容。异常/暂时读不了 ≠ 库空了，
            # 把整屏刷成 0 会让人以为数据丢了。换根目录时 root 不同，照常应用空态。
            prev = getattr(self, "_last_snap", None)
            if prev is not None and prev.root == snap.root:
                return
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
        # R217：数据页改为按需刷新（见 _DB_PAGES）。刷新时用户正看着的那页立即兑现，
        # 其余标脏等切页——否则每次 apply 都要把 2s+ 的重活压在不可见页上。
        self._db_dirty = set(self._DB_PAGES)
        _cur_attr = self._page_attr(self.stackedWidget.currentWidget())
        # force=True：刷新意味着「刚才那次的库状态已经不作数了」，此时在飞的读取要作废，
        # 由这次刷新重新发起——序号一变，旧回调自动被丢（见 _on_db_fetched）。
        if _cur_attr in self._db_dirty and self._reload_db_page(_cur_attr, force=True):
            self._db_dirty.discard(_cur_attr)
        self.audit_page.set_snapshot(snap)
        self.overview_page.set_snapshot(snap)
        try:
            self.overview_page.set_db(brain.stats(self.root), brain.recent_diverse(self.root, limit=8))
            # 展示窗口 24h（在线判定仍是缺省 30 分钟 TTL；2026-10-11：短窗口下该面板常显空）
            self.overview_page.set_sessions(brain.active_sessions(self.root, ttl=24 * 3600))
        except Exception:
            self.overview_page.set_sessions([])
        # R772：以上两页直接读库，根目录失效时抛 sqlite 错误会从 Qt 槽里冒出来、
        # 把整次 apply 掐断在中间（后面的页全不更新、_apply_count 也不涨）。
        # 失败要可见但不能让一次刷新半途死掉；成功一次后自动解除只提示一次的限制。
        try:
            self.journal_page.reload()
            self.report_page.reload_reports()  # agent 侧 hub_report 可能生成了新报告，F5 后列表跟上
        except Exception as e:  # noqa: BLE001
            if not getattr(self, "_secondary_fail_warned", False):
                self._secondary_fail_warned = True
                InfoBar.warning("部分页面未能刷新", f"流水/报告读取失败：{e}",
                                duration=6000, parent=self)
        else:
            self._secondary_fail_warned = False
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
        self._apply_count += 1
        self._last_snap = snap


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
_SINGLE_INSTANCE = None  # R205 单实例管道（QLocalServer 必须留引用，否则被 GC 后新实例又能起）


def _claim_single_instance(app) -> bool:
    """R205：同一时间只跑一个 GUI 实例（双开会抢热键/托盘/DB 写入并互相踩刷新）。

    已有一个实例 → 让它把窗口抬到最前并返回 False；否则占用本用户名下的管道。
    """
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    name = "agenthub-gui-" + (os.environ.get("USERNAME") or "user")
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.flush()
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return False
    QLocalServer.removeServer(name)  # 上个实例崩溃/硬杀留下的死管道
    global _SINGLE_INSTANCE
    _SINGLE_INSTANCE = QLocalServer(app)
    _SINGLE_INSTANCE.listen(name)  # 拿不到锁也照常启动：可用性优先，最坏退回旧行为
    return True


def run_gui():
    global _THEME_LISTENER
    _install_crash_hook()
    tokens.apply_theme(tokens.load_theme_choice())  # 主题：两段式应用（覆盖 qfw setTheme 短路）
    # 高分屏缩放策略要在 QApplication 之前设，故在此先调一次
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    if not _claim_single_instance(app):
        # R215：第二实例静默退出（_claim 已通过管道请既有窗口抬到最前）。
        # 不要弹模态框——实测会让第二个进程卡在对话框等用户点确定。
        return
    _setup_app_font(app)
    w = AgentHubWindow()
    w.show()

    def _raise_existing() -> None:
        while _SINGLE_INSTANCE and _SINGLE_INSTANCE.hasPendingConnections():
            conn = _SINGLE_INSTANCE.nextPendingConnection()
            if conn:
                conn.disconnectFromServer()
        w.showNormal()
        w.raise_()
        w.activateWindow()

    _SINGLE_INSTANCE.newConnection.connect(_raise_existing)
    _THEME_LISTENER = tokens.start_system_listener()  # 系统深浅切换联动（仅 auto 模式响应）
    _unreg = _register_note_hotkey(w)
    if _unreg:
        app.aboutToQuit.connect(_unreg)
    rc = app.exec()
    _hard_exit(rc)  # qfw item view 退出挂起兜底（已知坑）；_hard_exit 另消 detached 线程段错误


if __name__ == "__main__":
    run_gui()
