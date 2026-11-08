# -*- coding: utf-8 -*-
"""gui 层：记忆列表标出「已被 #N 修正」（R979）。

为什么桌面也要标：注入 / hub_memory_read / hub_search 三个面都说了「已被 #370 修正，
冲突以它为准」，桌面列表却看不出来——同一个事实四个面两种说法，人会觉得 agent 在编。
真库实测量出来的场景：#71「GitHub 凭据全灭」被 #370 推翻、#495「DLSS 菜单消失=mod 失配」
被 #497 推翻，这两条都能在记忆列表里翻到。

边界：只是徽标，不是判决（不改数据、不改排序）；没有任何修正关系时列表一个字都不多。
"""
from helpers import boot_window, pump


def _row_for(page, mid):
    for i in range(page.memList.count()):
        if f"#{mid} " in page.memList.item(i).text():
            return i
    return -1


def _seed(root):
    import brain
    brain.init_db(root)
    old = brain.add_memory(root, "GUI 修正标注用例：旧结论是端口 8080",
                           kind="fact", agent="zcode")
    new = brain.add_memory(root, f"修正记忆#{old}：端口已改 9090",
                           kind="fact", agent="zcode")
    return old, new


def test_memory_row_marks_amended(qapp, hub_root):
    import tokens

    old, new = _seed(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.hub_page
    w.switchTo(page)
    page.viewCombo.setCurrentIndex(2)

    assert pump(qapp, lambda: _row_for(page, old) >= 0, 10), "记忆视图未加载出种子记忆"
    text = page.memList.item(_row_for(page, old)).text()
    assert f"已被#{new}修正" in text, f"旧条未标「已被新条修正」：{text!r}"
    # 修正者自己不该带徽标（它不是被推翻的那条）
    new_text = page.memList.item(_row_for(page, new)).text()
    assert "已被" not in new_text, f"新条不该带标注：{new_text!r}"


def test_memory_row_clean_without_amend(qapp, hub_root):
    """反证：没有修正关系时列表不许多一个字（否则每行都带噪音）。"""
    import brain
    import tokens

    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "一条普通的、没人修正过的记忆", kind="fact", agent="zcode")
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.hub_page
    w.switchTo(page)
    page.viewCombo.setCurrentIndex(2)

    assert pump(qapp, lambda: _row_for(page, mid) >= 0, 10)
    text = page.memList.item(_row_for(page, mid)).text()
    assert "已被" not in text, f"无修正关系却带了标注：{text!r}"


def test_badge_has_no_forbidden_glyph(qapp, hub_root):
    """R984：徽标里的符号必须在生产字体里有字形。

    R979 首版用了 ⚠（U+26A0）——它在 test_glyph_coverage 的 FORBIDDEN 里，真机显示方框。
    那条字形用例扫不到这里：它跑的是种子库，而种子库没有「被修正」的记忆，徽标根本不出现。
    这条补上：有标注时，整行文本里不许出现禁用符号。
    """
    import tokens
    from test_glyph_coverage import FORBIDDEN

    old, new = _seed(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.hub_page
    w.switchTo(page)
    page.viewCombo.setCurrentIndex(2)

    assert pump(qapp, lambda: _row_for(page, old) >= 0, 10)
    text = page.memList.item(_row_for(page, old)).text()
    hit = [ch for ch in text if ch in FORBIDDEN]
    assert not hit, f"徽标用了字体没字形的符号 {hit}：{text!r}"
