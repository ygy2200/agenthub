# -*- coding: utf-8 -*-
"""AgentHub 兼容出口（P2-3 收口后：本文件只做再导出 + 启动入口）。

拆分后的真实位置：
  - uikit.py  —— 共享基建（图标/徽章/对话框/workers/主题…）
  - pages/    —— 各页面（每页一文件）
  - window.py —— 主窗口 + 启动（run_gui）
  - 本文件    —— 兼容出口：外部（启动脚本/工具/测试）继续 `import ui` 取一切
"""
from __future__ import annotations

from uikit import *  # noqa: F401,F403  共享基建再导出（旧引用 ui.X 全部兼容）
from uikit import __all__ as _uikit_all  # noqa: F401
import actions  # noqa: F401

from pages.project_page import ProjectPage  # noqa: F401
from pages.today_page import TodayPage  # noqa: F401
from pages.signal_page import SignalPage  # noqa: F401
from pages.governance_page import GovernancePage  # noqa: F401
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

from window import AgentHubWindow, run_gui, _hard_exit  # noqa: F401


if __name__ == "__main__":
    run_gui()
