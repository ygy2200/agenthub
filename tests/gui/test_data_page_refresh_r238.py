# -*- coding: utf-8 -*-
"""R238：数据页「点开空白、得走开再回来才有数据」的三个不变量。

真实库实测（output/_r238_probe.txt）：首轮 apply 在窗口出现后 887ms 才落地，而
`_db_dirty` 只在 apply() 里创建——这之前切到时间线/统计等 7 个数据页时，`switchTo`
走 `hasattr(self, "_db_dirty")` 为假的分支，既不加脏也不加载。同时
`switchTo` 里 `super().switchTo(page)` 让页面**先上屏**、之后才同步加载
（治理 509ms / 大脑 402ms / 统计 354ms），于是用户看到的是一帧空白再蹦出数据；
更糟的是脏标记在加载**之前**就被 discard，而 `_reload_db_page` 吞异常——
库还没就绪或 root 还没选时，标记被消掉、数据没填上，这一页就永远空白到下一次全局刷新。

钉三件事：① 脏名单从构造起就存在；② 只有真填上才算填过（失败/无 root 保留标记）；
③ 数据在页面上屏之前填好（不许出现空白帧）。

R239 修订：③ 对 `_DB_ASYNC` 里的页改成了「上屏时有东西——数据或真忙态」。判据是实测
（output/_r239_dupes.txt）：统计页 383ms 墙钟里取数 345ms、画控件只有 38ms，把取数提到
上屏之前等于让切页硬冻 0.4s。同步页仍按 ③ 原样钉，异步页由
tests/gui/test_async_stats_fetch_r239.py 钉忙态先行 + 落地才消脏。

R243 修订：大脑页也进了 `_DB_ASYNC`（其 ③ 由 tests/gui/test_brain_page_async_r243.py
钉），③ 的同步样本改用今日页——`_DB_ASYNC` 之外的页仍走「先 reload 再 super().switchTo」，
这条口径不能因为样本页搬家就没人守。
"""
import pytest

from helpers import boot_window, pump, settle


@pytest.fixture
def window(qapp, hub_root):
    import brain
    brain.init_db(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        yield w
    finally:
        w.hide()


def test_dirty_list_exists_before_first_apply(window):
    """构造完、事件循环还没跑时 apply() 尚未发生：脏名单此时就必须已经在。"""
    assert hasattr(window, "_db_dirty"), (
        "_db_dirty 只在 apply() 里创建，首轮扫描落地前的切页既不加脏也不加载")
    assert set(window._db_dirty) == set(window._DB_PAGES)


def test_failed_load_keeps_page_dirty(window, qapp):
    """加载抛异常时不许把脏标记消掉——否则这页永远空白到下次全局刷新。"""
    import brain
    settle(qapp, max_s=6.0)
    assert "stats_page" in window._db_dirty, "用例前提：首轮刷新只填当前页"
    orig = brain.stats
    brain.stats = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("库未就绪"))
    try:
        window.switchTo(window.stats_page)
        assert "stats_page" in window._db_dirty, (
            "加载失败后脏标记被 discard，这页再切回来也不会重试（除非等下一次全局刷新）")
        assert window._reload_db_page("stats_page") is False
    finally:
        brain.stats = orig


def test_no_root_does_not_consume_dirty(window, qapp):
    """还没选目录时切数据页：不该假装加载过，也不该调数据库。"""
    import brain
    settle(qapp, max_s=6.0)
    calls = []
    orig = brain.stats
    brain.stats = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    try:
        window.root = ""
        assert window._reload_db_page("stats_page") is False
        window.switchTo(window.stats_page)
        assert "stats_page" in window._db_dirty
        assert calls == [], "root 为空时不该去读库（读出来必然是空的假状态）"
    finally:
        brain.stats = orig


def test_data_lands_before_page_is_shown(window, qapp):
    """同步加载的页：数据必须在页面上屏**之前**填进去，之后填就是先空白再蹦出来。

    R239 把统计页、R243 把大脑页挪进 `_DB_ASYNC`（实测取数占墙钟九成），那条路径做不到
    「上屏前填好」也不该做——它改成上屏前盖骨架屏，由 test_async_stats_fetch_r239.py /
    test_brain_page_async_r243.py 钉。本用例因此换到仍是同步加载的今日页：
    `switchTo` 里先 reload 再 super().switchTo。
    """
    settle(qapp, max_s=6.0)
    seen = []
    page = window.today_page
    orig = page.reload

    def spy(*a, **k):
        seen.append(page.isVisible())
        return orig(*a, **k)

    page.reload = spy
    try:
        window.switchTo(window.today_page)
    finally:
        page.reload = orig
    assert seen, "切到今日页一次都没有填数据"
    assert seen[-1] is False, (
        "填数据时页面已经可见（先空白后蹦出）；应先 _reload_db_page 再 super().switchTo")


def test_successful_load_clears_dirty(window, qapp):
    """反过来也要钉住：真填上了就该消脏，不能每次切页都重付一遍加载成本。"""
    settle(qapp, max_s=6.0)
    window.switchTo(window.stats_page)
    assert pump(qapp, lambda: "stats_page" not in window._db_dirty, timeout=10.0), (
        "R239 起统计页取数在后台线程：落地后才消脏，这里要等它")
    window.switchTo(window.overview_page)
    window.switchTo(window.stats_page)
    assert "stats_page" not in window._db_dirty
