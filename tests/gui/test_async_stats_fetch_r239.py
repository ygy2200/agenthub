# -*- coding: utf-8 -*-
"""R239：统计页的取数挪出 UI 线程，忙态占位顶上 R238 的「上屏前必须填好」。

真实库实测（output/_r239_dupes.txt）：`_reload_db_page("stats_page")` 墙钟 383ms，
其中非取数（建控件/排版）只有 38ms——96% 是数据库。大脑页 375ms/13ms、治理页
489ms/3ms 同一个形状。所以这三页的「卡」根本不是绘制，是 UI 线程在等 SQLite + 纯
Python 相似度计算；R238 把取数提到上屏之前，等于把这段等待**整段加进切页手感**里
（切过去就是硬冻 0.4s，期间窗口不重绘、不响应点击）。

正解按本项目已有的 FnWorker 惯例（timeline/search/report/settings/hub 五页早就这么干）
把取数放后台线程：上屏时页面上是骨架屏（真忙态，不是空白），数据回到主线程再填。
于是 R238 的不变量从「数据在上屏前填好」修订为「上屏时页面上必须有东西——数据或忙态」，
脏标记仍然只有真填上才消。

钉八件事：① 取数不在主线程；② 在飞期间有真忙态且没有半截数据；③ 脏标记只在数据落地后消；
④ 取数失败保留脏标记并说明失败（不许静默空白）；⑤ 全局刷新作废在飞的旧读取，只渲染新结果；
⑥ root 为空时不起线程、不读库；⑦ 在飞线程登记进 `_collect_worker_lists`（否则用例清理与
退出等待都看不见它，而带任务的 QThread 被析构会把整个进程弄成半死）；⑧ `apply_db` 一次库
都不碰——这是「取数能整体上后台」的前提。
"""
import threading

import pytest
from PySide6.QtWidgets import QLabel

from helpers import boot_window, pump, settle
from uikit import Skeleton


@pytest.fixture
def window(qapp, hub_root):
    import brain
    brain.init_db(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        yield w
    finally:
        w.hide()


def _bundle(marker):
    """只含 stats 的最小包：health/tasks 走 None 分支，用例不依赖体检内容。"""
    return {"stats": {"projects": 1, "records": marker, "errors_open": 0,
                      "projects_stalled": 0, "searches_today": 0, "searches_total": 0,
                      "agent_counts": {}, "monthly_counts": {}},
            "health": None, "distill": 0, "tasks": None}


def _busy(page):
    return [w for w in page.findChildren(Skeleton) if w.isVisibleTo(page)]


def _fail_text(page):
    return [x for x in page.findChildren(QLabel) if "加载失败" in x.text()]


def _blocked_fetch(page, marker, gate, hits):
    """把 fetch_db 换成阻塞在 gate 上的假取数：放行前不返回，并记下调用线程。"""
    orig = page.fetch_db

    def _fetch(root):
        hits.append(threading.get_ident())
        gate.wait(20.0)
        return _bundle(marker)

    page.fetch_db = _fetch
    return orig


def _restore(page, orig):
    page.fetch_db = orig


def test_stats_fetch_runs_off_the_ui_thread(window, qapp):
    """取数必须在别的线程：主线程等 383ms 就是用户手里的窗口冻 383ms。"""
    settle(qapp, max_s=6.0)
    page = window.stats_page
    gate, hits = threading.Event(), []
    orig = _blocked_fetch(page, 7, gate, hits)
    try:
        window.switchTo(window.stats_page)
        assert pump(qapp, lambda: hits, timeout=8.0), "切到统计页一次都没发起取数"
        assert hits[-1] != threading.get_ident(), (
            "取数还在 UI 线程跑：真实库实测 stats 345ms 里 96% 是数据库等待")
    finally:
        gate.set()
        _restore(page, orig)
        pump(qapp, lambda: "stats_page" not in window._db_dirty, timeout=8.0)


def test_busy_placeholder_is_on_screen_while_fetching(window, qapp):
    """在飞期间页面上必须有真忙态，且数字没被半截填上。"""
    settle(qapp, max_s=6.0)
    page = window.stats_page
    gate, hits = threading.Event(), []
    orig = _blocked_fetch(page, 9, gate, hits)
    try:
        window.switchTo(window.stats_page)
        assert pump(qapp, lambda: hits, timeout=8.0)
        assert _busy(page), "取数在飞时统计页没有可见骨架屏（用户看到的是空白）"
        assert page.valRec.text() == "0", "数据还没到就把数字填上了（半截渲染）"
    finally:
        gate.set()
        _restore(page, orig)
        assert pump(qapp, lambda: page.valRec.text() == "9", timeout=8.0)
        assert not _busy(page), "数据落地后骨架屏还盖在页面上"


def test_dirty_flag_clears_only_when_data_lands(window, qapp):
    """异步加载的「消脏」责任在回调：在飞时保留，落地后才消。"""
    settle(qapp, max_s=6.0)
    page = window.stats_page
    gate, hits = threading.Event(), []
    orig = _blocked_fetch(page, 12, gate, hits)
    try:
        window.switchTo(window.stats_page)
        assert pump(qapp, lambda: hits, timeout=8.0)
        qapp.processEvents()
        assert "stats_page" in window._db_dirty, (
            "数据还没落地就消脏：这一页再也不会被补")
    finally:
        gate.set()
        _restore(page, orig)
        assert pump(qapp, lambda: "stats_page" not in window._db_dirty, timeout=8.0)


def test_failed_fetch_keeps_dirty_and_shows_failure(window, qapp):
    """取数炸了：脏标记留着（下次切页重试），页面上要说得清是失败而不是没数据。"""
    settle(qapp, max_s=6.0)
    page = window.stats_page
    orig = page.fetch_db
    page.fetch_db = lambda root: (_ for _ in ()).throw(RuntimeError("库被占用"))
    try:
        window.switchTo(window.stats_page)
        assert pump(qapp, lambda: _fail_text(page), timeout=10.0), (
            "取数失败后页面上没有失败说明——用户分不清「没数据」和「坏了」")
        assert "stats_page" in window._db_dirty, "失败却消脏：这页会一直空到下次全局刷新"
        assert not _busy(page), "已经报失败了还挂着忙态"
    finally:
        _restore(page, orig)


def test_refresh_voids_a_fetch_that_is_still_in_flight(window, qapp):
    """刷新时在飞的旧读取必须作废：否则旧结果后到就把新数据盖掉。"""
    settle(qapp, max_s=6.0)
    page = window.stats_page
    g1, g2, hits = threading.Event(), threading.Event(), []
    orig = page.fetch_db

    def _fetch(root):
        n = len(hits) + 1
        hits.append(n)
        (g1 if n == 1 else g2).wait(20.0)
        return _bundle(111 if n == 1 else 222)

    page.fetch_db = _fetch
    try:
        window.switchTo(window.stats_page)
        assert pump(qapp, lambda: len(hits) == 1, timeout=8.0)
        window.refresh()  # 全局刷新：作废 #1，为当前页重新发起 #2
        assert pump(qapp, lambda: len(hits) == 2, timeout=12.0), (
            "刷新没给当前页重新发起取数——用户按刷新看到的还是刷新前的库")
        g1.set()          # 旧请求故意后到
        pump(qapp, lambda: False, timeout=0.4)
        g2.set()
        assert pump(qapp, lambda: "stats_page" not in window._db_dirty, timeout=8.0)
    finally:
        g1.set()
        g2.set()
        _restore(page, orig)
    assert page.valRec.text() == "222", "在飞的旧读取结果盖掉了刷新后的新结果"


def test_apply_db_touches_no_database(window, qapp):
    """渲染必须干净：把 brain 全部公开函数换成会抛异常的桩，apply_db 照样得填好页面。

    这条是异步化的地基——`set_db` 时代「渲染」里藏着 health_report / distill_candidates /
    task_metrics 三次读库（实测 345ms 里的主要部分），那种函数没法整体上后台线程。"""
    settle(qapp, max_s=6.0)
    import brain
    page = window.stats_page
    bundle = _bundle(4321)
    saved = {}
    for n in [k for k, v in vars(brain).items()
              if callable(v) and not k.startswith("_") and getattr(v, "__module__", "") == "brain"]:
        orig = getattr(brain, n)
        saved[n] = orig

        def _boom(*a, _n=n, **k):
            raise AssertionError(f"渲染阶段又去读库了：{_n}")

        setattr(brain, n, _boom)
    try:
        page.apply_db(bundle)
    finally:
        for n, v in saved.items():
            setattr(brain, n, v)
    assert page.valRec.text() == "4,321"
    assert page.healthLine.text() == "体检暂不可用"


def test_no_root_starts_no_worker(window, qapp):
    """没选目录时不该起线程、不该读库（读出来是「没有数据」的假状态）。"""
    settle(qapp, max_s=6.0)
    import brain
    calls = []
    orig = brain.stats
    brain.stats = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    try:
        window.root = ""
        assert window._reload_db_page("stats_page") is False
        window.switchTo(window.stats_page)
        assert "stats_page" in window._db_dirty
        assert calls == [], "root 为空时不该去读库"
        assert not [t for t in window._workers if t.isRunning()], "root 为空却起了取数线程"
    finally:
        brain.stats = orig


def test_inflight_worker_is_registered_for_shutdown_wait(window, qapp):
    """在飞线程要出现在 _collect_worker_lists 里，否则退出/清理会带着活线程析构窗口。"""
    settle(qapp, max_s=6.0)
    page = window.stats_page
    gate, hits = threading.Event(), []
    orig = _blocked_fetch(page, 5, gate, hits)
    try:
        window.switchTo(window.stats_page)
        assert pump(qapp, lambda: hits, timeout=8.0)
        assert [t for lst in window._collect_worker_lists() for t in lst if t.isRunning()], \
            "在飞的取数线程没登记进退出等待名单"
    finally:
        gate.set()
        _restore(page, orig)
        pump(qapp, lambda: not any(t.isRunning() for lst in window._collect_worker_lists()
                                   for t in lst), timeout=8.0)
