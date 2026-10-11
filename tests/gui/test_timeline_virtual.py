# -*- coding: utf-8 -*-
"""gui 层：时间线虚拟滚动（P2-3a）行为断言。"""
from helpers import boot_window, insert_records, pump, settle


def test_timeline_virtual_mode_and_paging(qapp, hub_root):
    import brain
    import tokens
    brain.init_db(hub_root)
    insert_records(hub_root, 260, projects=5)  # 260 > 200 阈值
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    tl = w.timeline_page
    w.switchTo(tl)

    assert pump(qapp, lambda: tl._virtual and tl._vstate
                and tl._vstate["offset"] >= 100 and not tl._vstate["loading"], 30), \
        f"虚拟模式首屏未就绪：virtual={tl._virtual} state={tl._vstate}"
    assert tl._vstate["total"] == 260
    assert not tl.all_entries, "虚拟模式不应把全量载入内存"

    # 分页到底（按钮路径）
    guard = 0
    while tl._vstate["offset"] < tl._vstate["total"] and guard < 8:
        before = tl._vstate["offset"]
        tl._vfetch()
        pump(qapp, lambda: tl._vstate["offset"] > before, 15)
        guard += 1
    assert tl._vstate["offset"] == 260, f"未翻到底：{tl._vstate}"

    # 过滤器下推：P2 → 52 条
    pf = tl.projFilter
    idx = pf.findText("P2")
    assert idx > 0, "项目过滤器未包含 P2"
    pf.setCurrentIndex(idx)
    assert pump(qapp, lambda: tl._vstate and not tl._vstate["loading"]
                and tl._vstate["total"] == 52, 15), f"过滤下推失败：{tl._vstate}"
    assert tl._vstate["offset"] <= 100


def test_timeline_legacy_mode_under_threshold(qapp, hub_root):
    """≤阈值：回旧路径（全量入内存 + 分批渲染），不启虚拟滚动。"""
    import brain
    brain.init_db(hub_root)
    insert_records(hub_root, 80, projects=2)
    w = boot_window(qapp, hub_root)
    tl = w.timeline_page
    w.switchTo(tl)
    assert pump(qapp, lambda: len(tl.all_entries) == 80, 30), \
        f"旧路径未生效：virtual={tl._virtual} n={len(tl.all_entries)}"
    assert not tl._virtual


def test_timeline_autoload_on_scroll(qapp, hub_root):
    """滚动到底 → 自动加载下一页（真实布局下）。"""
    import brain
    brain.init_db(hub_root)
    insert_records(hub_root, 260, projects=5)
    w = boot_window(qapp, hub_root)
    tl = w.timeline_page
    w.switchTo(tl)
    assert pump(qapp, lambda: tl._virtual and tl._vstate
                and tl._vstate["offset"] >= 100 and not tl._vstate["loading"], 30)
    settle(qapp, 6)
    bar = tl.scroll.verticalScrollBar()
    assert bar.maximum() > 0, "滚动条 range 未建立（布局未完成）"
    before = tl._vstate["offset"]
    bar.setValue(bar.maximum())
    assert pump(qapp, lambda: tl._vstate["offset"] > before, 15), \
        f"滚动到底未触发自动加载：{tl._vstate}"
