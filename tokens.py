# -*- coding: utf-8 -*-
"""AgentHub 设计 tokens —— 配色单一来源（UI 重构·批次1，2026-10-10）

设计依据（完整版：output/AgentHub-UI重构-设计学习集-2026-10-10.md）：
  · 中性色阶沿用课表 App 已验证体系（记录 #1778）；tertiary 修正为达标值（原值 2.7/2.9:1 ✗）
  · 全部色值经 WCAG 验算：正文 ≥4.5:1（所有修正项注明原值与原对比度）
  · accent = 图标靛蓝系（#4a5bd4 / #7b8ff0），与 assets/agenthub.ico 同源
  · 层级表达：底色差 > 1px 描边 > 投影（Fluent 2：Windows 用描边代替 key shadow）

用法：
    import tokens
    label.setStyleSheet(f"color:{tokens.C(tokens.TEXT_2)};")
    tokens.apply_theme("dark")         # 启动时 / 设置页切换时
    tokens.save_theme_choice("auto")   # 持久化（写 AgentHub 自己 config，不碰 qconfig 全局文件）
"""

from qfluentwidgets import qconfig, setTheme, Theme, isDarkTheme

import core

# ============ 中性色阶（浅/深双套） ============
BG_L0 = {"light": "#f9f9fb", "dark": "#161616"}    # 页面底
BG_L1 = {"light": "#ffffff", "dark": "#242424"}    # 卡片
BG_L2 = {"light": "#f5f5f7", "dark": "#2c2c2c"}    # 内嵌区/hover/代码块
STROKE = {"light": "rgba(0,0,0,0.08)", "dark": "rgba(255,255,255,0.08)"}  # 1px 分隔/描边
TEXT_1 = {"light": "#1b1b1f", "dark": "#f3f3f3"}   # 主文字    16.3:1 / 14.0:1
TEXT_2 = {"light": "#61616b", "dark": "#a4a4ac"}   # 次文字      6.1:1 / 6.3:1
TEXT_3 = {"light": "#6f6f78", "dark": "#8a8a94"}   # 弱提示      5.0:1 / 4.5:1（修正值）

# ============ accent（靛蓝，与图标同源） ============
ACCENT = {"light": "#4a5bd4", "dark": "#7b8ff0"}   # 文字/图标用：5.6:1 / 5.2:1
ACCENT_SOFT = {"light": "rgba(74,91,212,0.10)", "dark": "rgba(123,143,240,0.16)"}  # 选中行/胶囊淡底
WHITE = "#ffffff"

# ============ 语义色 ============
SUCCESS = {"light": "#0f7b0f", "dark": "#6ccb5f"}
WARNING = {"light": "#9d5d00", "dark": "#d9a300"}
DANGER = {"light": "#c50f1f", "dark": "#ff99a4"}
INFO = ACCENT

# ============ 徽章色（白字场景，单套不随主题；全部 ≥4.5:1） ============
BADGE_FALLBACK = "#8a8a8a"
AGENT_BADGE = {
    "zcode": "#0078d4", "hermes": "#8764b8", "deepseek": "#0a7ea4",
    "claude": "#b05832",     # 修正：原 #c76a42 白字 3.77:1 ✗
    "codex": "#4a6b8a",
    "dsh": "#0f7f5e",        # 修正：原 #00a67e 白字 3.11:1 ✗
    "workbuddy": "#a03e9e",  # 新增：原表缺失导致落灰兜底
    "其他": "#666666",
}
KIND_BADGE = {
    "lesson": "#b05832", "fact": "#0078d4", "preference": "#8764b8",
    "project": "#0f7f5e", "note": "#666666",
}
MOOD_BADGE = {  # 心情徽章（Daily_You 式）；原值全部达标，保留
    "兴奋": "#d13438", "笃定": "#107c10", "疑问": "#0078d4", "纠结": "#8764b8",
    "灵感": "#ca5010", "低落": "#5d5a58", "平静": "#4a6b8a",
}

# ============ 间距/圆角（4px 网格，对齐课表 #1780） ============
PAD_PAGE = 24
PAD_CARD = 16
GAP_CARD = 12
RADIUS_CTRL = 4
RADIUS_CARD = 8

# ============ 主题管理 ============
THEME_MODES = ("dark", "light", "auto")
DEFAULT_MODE = "dark"
MODE_CN = {"dark": "深色", "light": "浅色", "auto": "跟随系统"}


def C(spec):
    """取当前主题下的色值。spec 为 {"light","dark"} dict 或原样字符串。"""
    if isinstance(spec, dict):
        return spec.get("dark" if isDarkTheme() else "light", "")
    return spec


def agent_badge_color(agent):
    a = (agent or "").strip().lower()
    return AGENT_BADGE.get(a, BADGE_FALLBACK)


def kind_badge_color(kind):
    return KIND_BADGE.get(kind, BADGE_FALLBACK)


def mood_badge_color(mood):
    return MOOD_BADGE.get(mood, BADGE_FALLBACK)


def load_theme_choice() -> str:
    mode = str(core.load_config().get("theme_mode", DEFAULT_MODE)).strip().lower()
    return mode if mode in THEME_MODES else DEFAULT_MODE


def save_theme_choice(mode: str) -> None:
    cfg = core.load_config()
    cfg["theme_mode"] = mode
    core.save_config(cfg)


def apply_theme(mode: str) -> None:
    """应用主题。**强制两段式**，覆盖 qfw setTheme 的"值相同即短路"（设计学习集 §3.3）：
      ① qconfig.theme = target  —— 无条件同步运行时 _theme
      ② setTheme(target, save=False) —— 刷新样式表 + emit themeChangedFinished
         （FluentWindow 内建监听该信号刷新 Mica，无需手动联动）
    save=False：不写 %APPDATA%\\qfluentwidgets\\config.json（全机 qfw 应用共享，防污染）。"""
    mode = (mode or DEFAULT_MODE).strip().lower()
    if mode not in THEME_MODES:
        mode = DEFAULT_MODE
    target = {"dark": Theme.DARK, "light": Theme.LIGHT, "auto": Theme.AUTO}[mode]
    qconfig.theme = target              # ① _theme 同步（AUTO 时内部做系统检测）
    setTheme(target, save=False)        # ② 样式刷新 + 信号（Mica 随之更新）


def start_system_listener(parent=None):
    """启动系统主题监听（auto 模式联动）。始终启动——其内部仅在 themeMode=AUTO 时响应。
    返回 listener 实例（调用方需保引用；退出由进程硬杀兜底，无需 stop）。"""
    try:
        from qfluentwidgets import SystemThemeListener
        lis = SystemThemeListener(parent)

        def _on_sys_change():
            if load_theme_choice() == "auto":
                apply_theme("auto")
        lis.systemThemeChanged.connect(_on_sys_change)
        lis.start()
        return lis
    except Exception:
        return None
