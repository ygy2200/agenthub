# -*- coding: utf-8 -*-
"""GUI 字形覆盖守卫（R165）：状态符号不得渲染成方框。

2026-10-26 实测：qfluentwidgets 的 CaptionLabel/BodyLabel 使用 Segoe UI，
✓ ✗ ⚠ 在该字体的度量里不存在，实际渲染为 □——接入页「✓ 已接入大脑」、
治理页「✓ 安全体检」在真实窗口里都变成了方框。中文经由系统字体链接正常回退，
符号却没有，因此不能靠「中文能显示」推断符号也能显示。

守卫分两档：
  · 静态控件（标签/按钮/输入框/页签/提示）：检查所有 So/Sm/Sk 类缺字形字符；
  · 列表/表格/下拉项（可能承载技能描述、记录标题等外部数据）：只查已知危险符号，
    避免把第三方内容里的 emoji 当成 UI 缺陷。
"""
import os
import unicodedata

from helpers import boot_window, pump, settle

GLYPH_CATEGORIES = {"So", "Sm", "Sk"}
# 生产字体缺失、且 UI 曾实际使用过的状态符号：一旦出现即渲染成方框
FORBIDDEN = set("✓✗⚠✔✘ⓘ▶◀⏸⏵")
SAFE_REPLACEMENTS = {
    "✓": "● 或 √", "✔": "● 或 √", "✗": "×", "✘": "×",
    "⚠": "！或「注意：」", "ⓘ": "用图标或文字代替",
    "▶": "→ 或「运行」", "◀": "←", "⏸": "「暂停」", "⏵": "→",
}
ITEM_VIEW_TYPES = {"ListWidget", "TreeWidget", "TableWidget", "ComboBox", "QListWidget",
                   "QTreeWidget", "QTableWidget", "QComboBox"}


def _load_production_font():
    """测试进程默认字体库与生产不同：不加载会导致 ●/｜ 等正常字形被误报。"""
    from PySide6.QtGui import QFontDatabase
    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"):
        if os.path.exists(path):
            QFontDatabase.addApplicationFont(path)
            return


def _texts_of(wd):
    out = []
    for getter in ("text", "placeholderText", "toolTip"):
        try:
            t = getattr(wd, getter)()
        except Exception:  # noqa: BLE001 某些控件的 text() 需要参数
            continue
        if isinstance(t, str) and t:
            out.append(t)
    try:
        t = wd.toPlainText()
    except Exception:  # noqa: BLE001
        t = ""
    if isinstance(t, str) and t:
        out.append(t)
    try:
        for i in range(wd.count()):
            it = wd.item(i)
            if it is not None and hasattr(it, "text"):
                out.append(it.text())
    except Exception:  # noqa: BLE001 非 item view
        pass
    try:
        for i in range(wd.count()):
            t = wd.itemText(i)
            if t:
                out.append(t)
    except Exception:  # noqa: BLE001 非 combo/box
        pass
    return out


def _missing_symbols(font, text, only_forbidden=False):
    from PySide6.QtGui import QFontMetrics
    fm = QFontMetrics(font)
    bad = set()
    for ch in set(text):
        if ch.isspace():
            continue
        if only_forbidden:
            if ch in FORBIDDEN:
                bad.add(ch)
            continue
        if unicodedata.category(ch) not in GLYPH_CATEGORIES:
            continue
        if not fm.inFont(ord(ch)):
            bad.add(ch)
    return bad


def test_visible_symbols_have_real_glyphs(qapp, hub_root):
    import actions
    import brain
    import tokens
    _load_production_font()
    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 40), \
        "扫描未完成"
    pump(qapp, lambda: bool(getattr(w.hub_page, "agents", None)), 20)
    bad = {}
    try:
        from PySide6.QtWidgets import QWidget
        for attr, title, *_rest in actions.PAGES:
            page = actions.page_of(w, attr)
            if page is None:
                continue
            w.switchTo(page)
            settle(qapp, max_s=3.0)
            for wd in page.findChildren(QWidget):
                if not wd.isVisible():
                    continue
                data_view = type(wd).__name__ in ITEM_VIEW_TYPES
                for text in _texts_of(wd):
                    for ch in _missing_symbols(wd.font(), text, only_forbidden=data_view):
                        bad.setdefault(ch, []).append(
                            (title, type(wd).__name__, text[:48].replace("\n", " ")))
    finally:
        # 主窗口 closeEvent 会排 2s 硬退出定时器，多用例同进程时会把后续用例截断
        w.hide()
    if bad:
        lines = []
        for ch, hits in sorted(bad.items()):
            hint = SAFE_REPLACEMENTS.get(ch, "换成字体已覆盖的符号或文字")
            lines.append(f"{ch} (U+{ord(ch):04X}) → {hint}")
            for t, cls, txt in hits[:3]:
                lines.append(f"    {t} · {cls} · {txt}")
        raise AssertionError("状态符号无字形，实际会显示为方框：\n" + "\n".join(lines))
