# -*- coding: utf-8 -*-
"""R243：大脑页的取数瘦身与「视图切换不读库」。

改前真实库实测（output/_r243_brain_cost.txt，best-of-3）：
  同步刷新 173ms = health_report 167ms（其中 similar_memories 125ms，本页从不渲染）
  + 置顶 140ms（search_memories("", "", 100) 再筛 pinned）
  「展开全部」= reload(force=True) → 428ms，只为多显示 5 行。

本文件钉产品层行为，不钉实现形状：
  · 切到大脑页时取数不在 UI 线程，页面上有真忙态，数据没落地不许填半截；
  · 六段内容一块不少（数字 + 唤起排行/使用分布/任务/待办/事件/置顶）；
  · 「展开全部」不再读库（调用计数为 0），且行数真的变了；
  · apply_db 一次 brain 调用都不发（取数能整体上后台的前提）；
  · 新写的置顶记忆下一次刷新必须出现在页面上（瘦身不许瘦掉数据）。
"""
import threading

import pytest
from PySide6.QtWidgets import QWidget

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


def _seed_pushed(root, n=10):
    """灌 n 条被打过的记忆（use_count>0）→ 唤起排行有内容，可测展开。"""
    import brain
    with brain.db_conn(root) as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO memories(kind,content,project,agent,pinned,status,created,"
                "updated,use_count,maturity) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("fact", f"被推送的记忆 {i}", "P1", "qoder", 0, "active",
                 "2026-10-20T10:00:00", "2026-10-20T10:00:00", n - i, "draft"))


def _busy(page):
    return [w for w in page.findChildren(Skeleton) if w.isVisibleTo(page)]


def test_brain_page_joins_async_set(window):
    page = window.brain_page
    assert "brain_page" in window._DB_ASYNC
    for name in ("fetch_db", "apply_db", "show_busy", "show_failure"):
        assert callable(getattr(page, name, None)), f"缺异步取数契约 {name}"


def _box_texts(box):
    """布局项里的文字：行控件要等布局激活才有父级，findChildren 得从行控件本身找。"""
    out = []
    for i in range(box.count()):
        wid = box.itemAt(i).widget()
        if wid is None:
            continue
        for x in [wid] + wid.findChildren(QWidget):
            t = getattr(x, "text", None)
            if callable(t):
                try:
                    out.append(t())
                except Exception:  # noqa: BLE001 个别标签的 text 需要参数
                    pass
    return out


def test_fetch_runs_off_the_ui_thread_with_busy_placeholder(window, qapp, hub_root):
    """取数必须在别的线程，且在飞期间屏幕上必须是真忙态而不是半截数据。"""
    import brain
    # 空库 memories=0，会把「没填」和「填了 0」混为一谈；先灌一条记忆（notes 不算 memories）
    with brain.db_conn(hub_root) as conn:
        conn.execute(
            "INSERT INTO memories(kind,content,project,agent,pinned,status,created,"
            "updated,use_count,maturity) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("fact", "R243 有数据", "P1", "qoder", 0, "active",
             "2026-10-20T10:00:00", "2026-10-20T10:00:00", 1, "draft"))
    settle(qapp, max_s=6.0)
    page = window.brain_page
    gate, hits = threading.Event(), []
    orig = page.fetch_db

    def _fetch(root):
        hits.append(threading.get_ident())
        gate.wait(20.0)
        return orig(root)

    page.fetch_db = _fetch
    try:
        window.switchTo(page)
        assert pump(qapp, lambda: hits, timeout=10.0), "切到大脑页一次都没发起取数"
        assert hits[-1] != threading.get_ident(), "取数还在 UI 线程跑（改前 173ms 硬冻）"
        assert _busy(page), "取数在飞时大脑页没有可见骨架屏"
    finally:
        gate.set()
        page.fetch_db = orig
        assert pump(qapp, lambda: "brain_page" not in window._db_dirty, timeout=10.0)
        settle(qapp, max_s=4.0)
        assert not _busy(page), "数据落地后骨架屏还盖着"
        assert page.valMem.text() not in ("", "0"), "数据落地了但数字没填"


def test_all_six_sections_have_content(window, qapp, hub_root):
    _seed_pushed(hub_root)
    page = window.brain_page
    page.reload(force=True)
    app = qapp
    app.processEvents()
    for name in ("topBox", "useBox", "taskBox", "todoBox", "eventBox", "pinBox"):
        box = getattr(page, name)
        assert box.count() > 0, f"{name} 空了——瘦身把内容削掉了"
    assert page.valMem.text() != "0"
    assert page.topBox.count() == 5, "唤起排行默认应显示前 5 条"


def test_toggle_reads_no_database(window, qapp, hub_root, monkeypatch):
    """「展开全部」是纯视图动作：改前每次整次重读 428ms。"""
    import brain
    _seed_pushed(hub_root)
    page = window.brain_page
    page.reload(force=True)
    qapp.processEvents()
    assert page.topBox.count() == 5

    calls = []

    def spy(name, real):
        def inner(*a, **kw):
            calls.append(name)
            return real(*a, **kw)
        return inner

    for n in ("health_report", "list_pinned_memories", "search_memories",
              "task_list", "task_annotate", "file_lease_list", "event_list"):
        monkeypatch.setattr(brain, n, spy(n, getattr(brain, n)))
    page._toggle_top()
    qapp.processEvents()
    assert calls == [], f"展开/收起又去读库了：{sorted(set(calls))}"
    assert page.topBox.count() == 10, "展开后没显示全部 10 条"
    page._toggle_top()
    qapp.processEvents()
    assert page.topBox.count() == 5, "收起后应回到前 5 条"


def test_apply_db_never_touches_brain(window, qapp, hub_root, monkeypatch):
    """渲染只许碰控件：取数能整体上后台的前提。"""
    import brain
    page = window.brain_page
    bundle = page.fetch_db(hub_root)

    def boom(*a, **kw):
        raise AssertionError("apply_db 里又读库了")

    for n in ("health_report", "list_pinned_memories", "search_memories", "task_list",
              "task_annotate", "file_lease_list", "event_list", "stats"):
        monkeypatch.setattr(brain, n, boom)
    page.apply_db(bundle, force=True)
    qapp.processEvents()
    assert page.valMem.text() != ""


def test_new_pinned_memory_appears_on_next_refresh(window, qapp, hub_root):
    """瘦身不许瘦掉数据：新写的置顶下一次刷新必须出现在置顶段。"""
    import brain
    page = window.brain_page
    page.reload(force=True)
    qapp.processEvents()
    before = page.pinBox.count()
    with brain.db_conn(hub_root) as conn:
        conn.execute(
            "INSERT INTO memories(kind,content,project,agent,pinned,status,created,"
            "updated,use_count,maturity) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("lesson", "新写的置顶记忆 R243", "P1", "qoder", 1, "active",
             "2026-10-28T10:00:00", "2026-10-28T10:00:00", 1, "draft"))
    page.reload()
    qapp.processEvents()
    texts = _box_texts(page.pinBox)
    assert any("新写的置顶记忆 R243" in t for t in texts), "新增置顶没出现"
    assert page.pinBox.count() >= before
