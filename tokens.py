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

from qfluentwidgets import qconfig, setTheme, setThemeColor, Theme, isDarkTheme

import core
import sys

# ============ 中性色阶（浅/深双套） ============
BG_L0 = {"light": "#f9f9fb", "dark": "#161616"}    # 页面底
BG_L1 = {"light": "#ffffff", "dark": "#242424"}    # 卡片
BG_L2 = {"light": "#f5f5f7", "dark": "#2c2c2c"}    # 内嵌区/hover/代码块
# 2026-10-15 第二轮：新增第 4 档表面给浮层（下拉/菜单/对话框/Tooltip）。
# 依据：深色模式"越高越亮"，至少 4 档表面；此前只有 3 档，浮层只能落 L1/L2 与卡片同色，
# 深色下边界糊成一片。候选值实测：#3a3a3a 对 L2 #2c2c2c = 1.23:1、对 L0 = 1.59:1
# （可辨层级但不抢戏）。浅色侧浮层用纯白，比卡片（#f5f5f7）更亮，方向一致。
BG_L3 = {"light": "#ffffff", "dark": "#3a3a3a"}   # 浮层：菜单/下拉/对话框/Tooltip
STROKE = {"light": "rgba(0,0,0,0.08)", "dark": "rgba(255,255,255,0.08)"}  # 1px 分隔/描边
# 行尾对比度数字由 `python tools/contrast_audit.py --write` 生成（勿手改）——
# 上一轮手写注释出现过"写着 5.0:1、实测 4.73:1"的自相矛盾。口径：light/dark 各自
# 对三档底色（L0/L1/L2）中的**最低值**。
TEXT_1 = {"light": "#1b1b1f", "dark": "#f3f3f3"}   # 15.77 / 12.59（各主题三档底色最低值，由 tools/contrast_audit.py --write 生成）
TEXT_2 = {"light": "#61616b", "dark": "#a4a4ac"}   # 5.62 / 5.64（各主题三档底色最低值，由 tools/contrast_audit.py --write 生成）
# TEXT_3 dark 原 #8a8a94：对 BG_L2 只有 4.09:1（<4.5 ✗，弹窗说明文字吃力）→ 提到 #96969f
TEXT_3 = {"light": "#6f6f78", "dark": "#96969f"}   # 4.57 / 4.76（各主题三档底色最低值，由 tools/contrast_audit.py --write 生成）

# ============ accent（靛蓝，与图标同源） ============
# 2026-10-15 第二轮：拆成"文字/图标色"与"填充色"两档。
#   为什么拆：深色模式下主色作为**文字/图标**要提亮（#7b8ff0 对 #161616 达 6.06:1），
#   但作为**按钮/选中填充**不能提亮——qfw 的 PrimaryPushButton/选中项/CheckBox 都是
#   "白字 + 主题色底"，白字在亮底上对比度塌方（#7b8ff0 白字仅 2.7:1 ✗）。
#   故填充色双主题同值 #4a5bd4（白字 5.62:1 ✓，对深色底 3.22:1 满足非文字 3:1 ✓）。
#   另：此前**从未调用过 qfw setThemeColor**，qfw 全量控件用的是 Windows 系统主题色
#   （本机实测 #009faa 青），与令牌的靛蓝各唱各调——见 apply_accent()。
ACCENT = {"light": "#4a5bd4", "dark": "#7b8ff0"}   # 文字/图标用：5.6:1 / 5.2:1
ACCENT_FILL = "#4a5bd4"      # 按钮/选中/勾选填充（双主题同值；白字 5.62:1）
ON_ACCENT = "#ffffff"       # 填充上的文字
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
# 2026-10-15 第二轮：再与"其他"统一为 #666666（白字 5.74:1）。#737373 的 4.74:1 仍是贴线值，
# 而它是未登记 agent / 未知 kind / 未知心情 / 随记页来源标签的兜底色——影响面最大，不该贴线。
BADGE_FALLBACK = "#666666"
AGENT_BADGE = {
    # 2026-10-15 第二轮：原值白字 4.53–4.63:1 贴 4.5 及格线（125% 缩放/低质屏下白字发虚）
    # → 加深到 ≥4.8:1（实测：zcode #0067bd 5.71 / hermes #6b4aa0 6.75 / deepseek #0a6a8a 6.10）
    "zcode": "#0067bd", "hermes": "#6b4aa0", "deepseek": "#0a6a8a",
    "claude": "#b05832",     # 修正：原 #c76a42 白字 3.77:1 ✗
    "codex": "#4a6b8a",
    "dsh": "#0f7f5e",        # 修正：原 #00a67e 白字 3.11:1 ✗
    "workbuddy": "#a03e9e",  # 新增：原表缺失导致落灰兜底
    "其他": "#666666",
}
KIND_BADGE = {
    "lesson": "#b05832", "fact": "#0067bd", "preference": "#6b4aa0",
    "project": "#0f7f5e", "note": "#666666",
}
MOOD_BADGE = {  # 心情徽章（Daily_You 式）；原值全部达标，保留
    "兴奋": "#d13438", "笃定": "#107c10", "疑问": "#0067bd", "纠结": "#6b4aa0",
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
# 2026-10-15 第二轮：深色加"边框亮度分级"作为第二信号——只靠底色差一种手段时，
# 卡片与内嵌区在小窗口/低对比屏上会糊在一起。三档描边对应低/中/高三种层级重量。
STROKE_CARD = {"light": "rgba(0,0,0,0.10)", "dark": "rgba(255,255,255,0.10)"}
STROKE_LEVELS = {
    "low":    {"light": "rgba(0,0,0,0.06)",  "dark": "rgba(255,255,255,0.06)"},
    "medium": {"light": "rgba(0,0,0,0.10)",  "dark": "rgba(255,255,255,0.10)"},
    "high":   {"light": "rgba(0,0,0,0.16)",  "dark": "rgba(255,255,255,0.16)"},
}
ELEVATION = ("e0", "e1", "e2", "e3")
# 焦点环（WCAG 2.2 SC 2.4.11 AA：焦点区≥2px 周长、与相邻色对比 ≥3:1）
# 深色 #8b9dff 对 #161616 = 7.21:1 / 对 #242424 = 6.18:1；浅色 #4a5bd4 对 #f9f9fb = 5.34:1。
FOCUS_RING = {"light": "#4a5bd4", "dark": "#8b9dff"}
FOCUS_RING_W = 2      # px，焦点环粗细（≥2px 才够 2.4.11 的面积要求）
FOCUS_RING_OFFSET = 2  # px，与控件边缘的间距（不压内容）

# ============ 字号阶梯（6 档；补上原 20→14 之间的 16 档） ============
# 原阶梯只有 28/20/14/14/12 五档，且"页面标题"与"数字卡数值"同为 28px 抢同一层级
# （2026-10-15 P2）。现在：页面标题降到 24，数字独立成 32px + tabular-nums。
# 元组 = (像素字号, 字重, 行高)。Qt Widgets 无 CSS line-height，行高只对多行控件生效。
TYPE = {
    "caption": (12, "normal", 16),
    "body": (14, "normal", 20),
    "body_strong": (16, "demibold", 22),
    "subtitle": (20, "demibold", 28),
    # 2026-10-15 第二轮：补 28 档（Fluent type ramp 的 Title）。此前 24 页头与 20 副标题
    # 只差 4px，详情面板标题/对话框标题/大分区标题无档位可用，只能"借"24 或降到 20。
    "heading": (28, "demibold", 36),   # 详情标题/对话框标题/大分区标题
    "title": (24, "demibold", 32),   # 页面标题（原 TitleLabel 28 让位给数字）
    "metric": (32, "demibold", 40),  # 数字卡数值
}
_WEIGHT = {"normal": 400, "medium": 500, "demibold": 600, "bold": 700}

# ============ 动效（克制的微交互：只用于"确认输入 / 引导注意"） ============
# 2026-10-15 第二轮：从"只有一档 200ms"扩成时长阶梯 + 曲线（Fluent motion 令牌口径：
# durationFast 150 / durationNormal 200，曲线 cubic-bezier(0.33, 0, 0.67, 1)）。
MOTION_FAST = 150   # ms，hover/按压反馈（Fluent durationFast）
MOTION_NORMAL = 200  # ms，面板/选中过渡
MOTION_OFF = 0      # 关闭动效（无障碍/低性能/系统关动画）
MOTION_CURVE = "cubic-bezier(0.33, 0, 0.67, 1)"   # Fluent easy-ease
# 2026 趋势要求尊重系统"减少动态效果"；Windows 侧对应
# HKCU\Control Panel\Desktop\WindowMetrics\MinAnimate == 0（关闭窗口动画）。
_SYS_MOTION_REG = r"Control Panel\Desktop\WindowMetrics"


def system_motion_off() -> bool:
    """读 Windows 动画开关：关 → True（调用方据此走瞬时切换）。读不到一律按"开"。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SYS_MOTION_REG) as k:
            v, _t = winreg.QueryValueEx(k, "MinAnimate")
        return str(v).strip() in ("0", "")
    except Exception:  # noqa: BLE001 老系统/无权限：按"开"处理，保持现状
        return False

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
    "bg_float": BG_L3,             # 浮层：菜单/下拉/对话框/Tooltip（第二轮新增）
    "stroke_default": STROKE,      # 1px 分隔/描边
    "stroke_low": STROKE_LEVELS["low"],      # 低层级描边
    "stroke_medium": STROKE_LEVELS["medium"],  # 中层级描边（卡片）
    "stroke_high": STROKE_LEVELS["high"],    # 高层级描边（浮层/选中）
    "text_primary": TEXT_1,        # 主文字
    "text_secondary": TEXT_2,      # 次文字
    "text_tertiary": TEXT_3,       # 弱提示
    "accent": ACCENT,              # 主色（文字/图标）
    "accent_fill": ACCENT_FILL,    # 主色（按钮/选中填充，单值）
    "on_accent": ON_ACCENT,        # 填充上的文字
    "accent_soft": ACCENT_SOFT,    # 选中行/胶囊淡底
    "focus_ring": FOCUS_RING,      # 焦点环（WCAG 2.2 2.4.11）
    "success": SUCCESS,
    "warning": WARNING,
    "danger": DANGER,
    "info": INFO,
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
    mode 传入 "light"/"dark" 时取指定主题（截图/测试用），缺省跟随当前主题。

    2026-10-15 复盘补充：调用方很自然会写 `resolve(tokens.ACCENT)`（传常量本身而非
    名字字符串），原先会撞进 `SEMANTIC.get(dict)` 抛 `TypeError: unhashable type: 'dict'`
    ——**入参问题伪装成内部错误**，栈指向库内部、看不出是自己传错了。现在交给 C() 兜底。
    """
    if not isinstance(name, str):
        return C(name)   # 传的是 {"light","dark"} 色值 dict / 其它对象：按 C() 语义解析
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
    f.setWeight(QFont.Weight(_WEIGHT.get(weight, 400)))  # PySide6 要 Weight 枚举，不能传裸 int
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


def motion_ms() -> int:
    """动效时长：配置关掉 / 系统关动画 → 0（调用方据此走"瞬时切换"分支）。"""
    try:
        if str(core.load_config().get("ui_motion", "on")).strip().lower() in ("off", "0", "false"):
            return MOTION_OFF
    except Exception:  # noqa: BLE001 配置读不到就按开
        pass
    if system_motion_off():
        return MOTION_OFF
    return MOTION_FAST


def easing():
    """动效曲线（Fluent easy-ease cubic-bezier(0.33, 0, 0.67, 1) 的 Qt 近似）。

    Qt 不接受 CSS 曲线字符串，用最接近的 InOutQuad；关动效时返回线性（配合 duration=0
    走瞬时分支，曲线无实际作用）。"""
    from PySide6.QtCore import QEasingCurve
    if motion_ms() == 0:
        return QEasingCurve(QEasingCurve.Type.Linear)
    return QEasingCurve(QEasingCurve.Type.InOutQuad)


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
    apply_accent()                      # ③ 主色与主题解耦：qfw 控件不跟系统 accent 跑


def apply_accent() -> None:
    """把设计令牌的填充主色喂给 qfw（2026-10-15 第二轮 P0）。

    根因：此前全仓**从未调用 setThemeColor**，qfw 的 PrimaryPushButton / 导航选中项 /
    CheckBox / ComboBox 高亮 / SegmentedWidget / ProgressBar / 焦点环一律用
    **Windows 系统主题色**（本机实测 #009faa 青），而设计令牌写的是靛蓝
    #4a5bd4/#7b8ff0 —— 同一屏里"主按钮青、徽章紫、图表青、图标靛蓝"，
    且换台机器（系统 accent 不同）界面就长得不一样，视觉回归基线无从建立。

    save=False：不写 %APPDATA%\\qfluentwidgets\\config.json（理由同 apply_theme）。
    幂等：每次切主题/启动都调一次，qfw 内部按值短路，无副作用。
    """
    try:
        setThemeColor(ACCENT_FILL, save=False, lazy=True)
    except TypeError:      # 老版本 qfw 无 lazy 参数
        setThemeColor(ACCENT_FILL, save=False)
    except Exception:  # noqa: BLE001 取不到接口就沿用系统色，不影响启动
        pass


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
