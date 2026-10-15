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
# 行尾对比度数字由 `python tools/contrast_audit.py --write` 生成（勿手改）——
# 上一轮手写注释出现过"写着 5.0:1、实测 4.73:1"的自相矛盾。口径：light/dark 各自
# 对三档底色（L0/L1/L2）中的**最低值**。
TEXT_1 = {"light": "#1b1b1f", "dark": "#f3f3f3"}   # 15.77 / 12.59（各主题三档底色最低值，由 tools/contrast_audit.py --write 生成）
TEXT_2 = {"light": "#61616b", "dark": "#a4a4ac"}   # 5.62 / 5.64（各主题三档底色最低值，由 tools/contrast_audit.py --write 生成）
# TEXT_3 dark 原 #8a8a94：对 BG_L2 只有 4.09:1（<4.5 ✗，弹窗说明文字吃力）→ 提到 #96969f
TEXT_3 = {"light": "#6f6f78", "dark": "#96969f"}   # 4.57 / 4.76（各主题三档底色最低值，由 tools/contrast_audit.py --write 生成）

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
# 2026-10-13 Round 3 修正：原 #8a8a8a 白字实测 3.45:1（<4.5 ✗），注释却写着"全部 ≥4.5:1"——
# 与实测矛盾且是唯一不达标项（其余 20 个徽章色均达标）。兜底色影响面最大：
# 未登记 agent / 未知 kind / 未知心情 / 随记页来源标签都落到这里。改为 #737373（4.74:1 ✓）。
BADGE_FALLBACK = "#737373"
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
    # 灵感：原 #ca5010 白字 4.50:1（贴线，缩放/低质屏下不可靠）→ 加深到 #b8480e（5.29:1）
    "灵感": "#b8480e", "低落": "#5d5a58", "平静": "#4a6b8a",
}

# ============ 间距（4px 网格，7 档；对齐 Fluent 2 spacingVertical 阶梯） ============
# 2026-10-15 P1：此前 PAD_*/GAP_*/RADIUS_* 定义了但**引用 0 次**，全仓散落 17 种边距
# 写法 / 10 种间距值（含 6、10 两个非网格值）。现在这里是唯一来源，页面一律引用常量，
# 并由 tests/gui/test_spacing_guard.py 扫描源码禁止再出现非白名单字面量。
SPACE_XS = 4      # 图标与文字、行内元素
SPACE_S = 8       # 行内间距、按钮之间
SPACE_M = 12      # 控件与标签、卡片之间
SPACE_L = 16      # 表面与内容
SPACE_XL = 20     # 宽卡内边距
SPACE_2XL = 24    # 页面外边距、区块之间
SPACE_3XL = 32    # 大分区

PAD_PAGE = SPACE_2XL      # 页面外边距 24
PAD_CARD = SPACE_L        # 卡片内边距 16（窄卡）
PAD_CARD_WIDE = SPACE_XL  # 卡片内边距 20（宽卡）
GAP_CARD = SPACE_M        # 卡片之间 12
GAP_SECTION = SPACE_2XL   # 区块之间 24
GAP_ROW = SPACE_S         # 列表行之间 8

# ============ 圆角（3 档） ============
RADIUS_CTRL = 4    # 控件：按钮/输入/下拉/分段
RADIUS_CARD = 8    # 容器：卡片/面板/对话框
RADIUS_PILL = 999  # 胶囊：按控件高度折算（见 pill_radius）；徽章/状态签专用

# ============ 层级（elevation）—— 只用"底色差 + 1px 描边"，不用投影 ============
# 依据：Fluent 2 在 Windows 上以描边代替 key shadow；且投影在深色底上会消失。
# e0=页面底（无描边） e1=卡片（1px 描边） e2=浮层/对话框（描边 + qfw 自带阴影）
STROKE_CARD = {"light": "rgba(0,0,0,0.10)", "dark": "rgba(255,255,255,0.10)"}
ELEVATION = ("e0", "e1", "e2")

# ============ 字号阶梯（6 档；补上原 20→14 之间的 16 档） ============
# 原阶梯只有 28/20/14/14/12 五档，且"页面标题"与"数字卡数值"同为 28px 抢同一层级
# （2026-10-15 P2）。现在：页面标题降到 24，数字独立成 32px + tabular-nums。
# 元组 = (像素字号, 字重, 行高)。Qt Widgets 无 CSS line-height，行高只对多行控件生效。
TYPE = {
    "caption": (12, "normal", 16),
    "body": (14, "normal", 20),
    "body_strong": (16, "demibold", 22),
    "subtitle": (20, "demibold", 28),
    "title": (24, "demibold", 32),   # 页面标题（原 TitleLabel 28 让位给数字）
    "metric": (32, "demibold", 40),  # 数字卡数值
}
_WEIGHT = {"normal": 400, "medium": 500, "demibold": 600, "bold": 700}

# ============ 密实度（P4：长会话工具偏紧凑，但也给舒适档） ============
DENSITY = {
    "compact": {"row_h": 32, "scale": 0.75},
    "comfortable": {"row_h": 44, "scale": 1.25},
}
DEFAULT_DENSITY = "comfortable"   # 与现状 36px 行高一致（36 介于两档之间，取 44 会太松）
ROW_H_BASE = 36                   # 现状基线行高（未开密度开关时沿用）
_curr_density = DEFAULT_DENSITY

# ============ 主题管理 ============
THEME_MODES = ("dark", "light", "auto")
DEFAULT_MODE = "dark"
MODE_CN = {"dark": "深色", "light": "浅色", "auto": "跟随系统"}


# ============ 语义 token 表（P2-3b 升级：组件只认语义名，根部统一解析） ============
# 命名约定：<场景>_<角色>。新组件一律用语义名（tokens.resolve("bg_surface") /
# tokens.C("text_primary")）；旧常量（BG_L0…）保留为唯一色值来源，语义表只是指向它们。
SEMANTIC = {
    "bg_page": BG_L0,              # 页面底
    "bg_surface": BG_L1,           # 卡片/浮层
    "bg_inset": BG_L2,             # 内嵌区/hover/代码块
    "stroke_default": STROKE,      # 1px 分隔/描边
    "text_primary": TEXT_1,        # 主文字
    "text_secondary": TEXT_2,      # 次文字
    "text_tertiary": TEXT_3,       # 弱提示
    "accent": ACCENT,              # 主色（文字/图标）
    "accent_soft": ACCENT_SOFT,    # 选中行/胶囊淡底
    "success": SUCCESS,
    "warning": WARNING,
    "danger": DANGER,
    "info": INFO,
    "on_accent": WHITE,            # 主色底上的文字（单套常量）
}


def _pick(spec, mode: str = ""):
    """色值选取：dict → 指定/当前主题；字符串 → 原样。"""
    if isinstance(spec, dict):
        if mode in ("light", "dark"):
            return spec.get(mode, "")
        return spec.get("dark" if isDarkTheme() else "light", "")
    return spec


def resolve(name: str, mode: str = "") -> str:
    """语义 token → 色值（根部统一解析）。
    name 可为语义名（bg_surface/text_primary…）或旧常量名（BG_L0…）；未知返回 ""。
    mode 传入 "light"/"dark" 时取指定主题（截图/测试用），缺省跟随当前主题。"""
    spec = SEMANTIC.get(name)
    if spec is None:
        spec = globals().get(name)
    if spec is None:
        return ""
    return _pick(spec, mode)


def C(spec):
    """取当前主题下的色值。spec 为 语义名 / {"light","dark"} dict / 原样字符串。"""
    if isinstance(spec, str) and spec in SEMANTIC:
        return resolve(spec)
    return _pick(spec)


def agent_badge_color(agent):
    a = (agent or "").strip().lower()
    return AGENT_BADGE.get(a, BADGE_FALLBACK)


def kind_badge_color(kind):
    return KIND_BADGE.get(kind, BADGE_FALLBACK)


def mood_badge_color(mood):
    return MOOD_BADGE.get(mood, BADGE_FALLBACK)


# ---------------------------------------------------------------- 派生的尺寸/排版助手


def pill_radius(height: int) -> int:
    """胶囊圆角：恒等于控件高度的一半（原徽章"高 20 + r=8"是方角胶囊，改成 10）。"""
    return max(2, int(height) // 2)


def stroke(level: str = "e1") -> str:
    """层级描边色：e0 无描边（返回 transparent），e1/e2 用 1px 描边。"""
    if level == "e0":
        return "transparent"
    return _pick(STROKE_CARD)


def typeset(widget, role: str) -> None:
    """按 TYPE 表给控件设字号/字重（P1：字号阶梯单一来源）。

    数字类（metric）额外开 tabular-nums——定宽数字，数值从 4 位变 5 位时不会把
    相邻卡片挤移位（Qt 侧用 QFont.setFixedPitch≈等宽近似，配合右对齐使用）。
    """
    spec = TYPE.get(role)
    if spec is None:
        return
    size, weight, _lh = spec
    from PySide6.QtGui import QFont
    f = widget.font()
    f.setPixelSize(size)
    f.setWeight(_WEIGHT.get(weight, 400))
    f.setFixedPitch(role == "metric")   # 定宽数字：读数不抖
    widget.setFont(f)


def set_density(mode: str) -> None:
    """切换密实度（compact/comfortable/basic）。影响行高与间距缩放系数。"""
    global _curr_density
    _curr_density = mode if mode in ("compact", "comfortable") else "basic"


def density() -> str:
    return _curr_density


def row_height() -> int:
    """当前密实度下的列表行高。"""
    return DENSITY.get(_curr_density, {}).get("row_h", ROW_H_BASE)


def gap(base: int) -> int:
    """按密实度缩放间距（并吸附回 4px 网格）。"""
    s = DENSITY.get(_curr_density, {}).get("scale", 1.0)
    v = int(round(base * s / 4.0) * 4)
    return max(SPACE_XS if base else 0, v)


def load_density_choice() -> str:
    """从配置读密实度（P4 设置页开关）。"""
    mode = str(core.load_config().get("ui_density", "basic")).strip().lower()
    return mode if mode in ("compact", "comfortable", "basic") else "basic"


def save_density_choice(mode: str) -> None:
    cfg = core.load_config()
    cfg["ui_density"] = mode if mode in ("compact", "comfortable", "basic") else "basic"
    core.save_config(cfg)


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
