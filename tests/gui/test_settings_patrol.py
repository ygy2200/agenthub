# -*- coding: utf-8 -*-
"""gui 层：设置页「系统自检」一键体检（R225）。

背景：一键自检（`ops.run_patrol` 20 项聚合）此前只存在于 `cli.py doctor`，帮助页甚至
写着"维护者用：不开界面也能自检"——等于承认界面里没有入口，用户遇到问题只能在文档和
终端之间来回跳。本文件守三件事：
  ① 结果真渲染出来（通过项 / 问题项 / 修复口径 / 跳过项都在）；
  ② 跑在后台线程（同步跑会把窗口冻住，而"卡住的体检"恰好是用户最需要它的时候）；
  ③ 体检器自己炸了也不把界面带崩——显示失败原因、按钮恢复可用。
"""
import threading

from helpers import boot_window, pump


def _fake_report():
    return {
        "ok": False, "checked": 3,
        "passed": ["env", "db_health"],
        "issues": [{"name": "audit_chain", "summary": "链尾截断 2 条",
                    "fix": "长口径：按运维手册 §4 重封存",
                    "fix_brief": "跑 hub_ops_run audit_chain 看断点",
                    "kind": "fix", "detail": ["第 12 行 hash 不匹配"]}],
        "skipped": [{"name": "regression", "why": "需要 target 目录，体检不代跑"}],
        "decide_count": 0, "fix_count": 1, "elapsed_ms": 42,
    }


def _open_settings(qapp, root):
    import actions
    w = boot_window(qapp, root)
    w.switchTo(actions.page_of(w, "settings_page"))
    return w, actions.page_of(w, "settings_page")


def test_patrol_runs_in_background_and_renders(qapp, hub_root, monkeypatch):
    import ops
    seen = {}

    def fake(root, progress=None):
        seen["thread"] = threading.current_thread()
        seen["root"] = root
        seen["progress"] = progress
        if progress is not None:
            progress(1, 3, "env")
        return _fake_report()

    monkeypatch.setattr(ops, "run_patrol", fake)
    w, page = _open_settings(qapp, hub_root)

    assert not page.patrolView.isVisible(), "没跑过时不该占版面"
    page.patrolBtn.click()
    assert pump(qapp, lambda: "audit_chain" in page.patrolView.toPlainText(), 10), \
        "自检结果未渲染"

    txt = page.patrolView.toPlainText()
    assert "✓ env" in txt and "✓ db_health" in txt, f"通过项缺失：{txt[:300]}"
    assert "✗ audit_chain" in txt and "链尾截断 2 条" in txt, f"问题项缺失：{txt[:300]}"
    assert "跑 hub_ops_run audit_chain 看断点" in txt, \
        "失败项必须带可执行修复口径（显示完整口径会淹没一行结论）"
    assert "regression" in txt and "跳过" in txt, f"跳过项缺失：{txt[:300]}"

    head = page.patrolState.text()
    assert "通过 2" in head and "问题 1" in head and "42ms" in head, f"结论行不对：{head}"
    assert page.patrolBtn.isEnabled(), "跑完必须恢复按钮"

    assert seen["thread"] is not threading.main_thread(), \
        "自检跑在主线程：真实库上 db_integrity 要读整库，窗口会冻住"
    assert seen["root"] == w.root
    assert callable(seen["progress"]), "没把进度回调交给 run_patrol（界面会 13 秒没有活信号）"


def test_patrol_failure_does_not_break_ui(qapp, hub_root, monkeypatch):
    """体检器自身异常（不是数据故障）要显示出来，且界面可用。"""
    import ops

    def boom(root, progress=None):
        raise RuntimeError("检查器坏了")

    monkeypatch.setattr(ops, "run_patrol", boom)
    w, page = _open_settings(qapp, hub_root)
    page.patrolBtn.click()
    assert pump(qapp, lambda: "检查器坏了" in page.patrolView.toPlainText(), 10), \
        "异常未反馈到界面"
    assert "失败" in page.patrolState.text()
    assert page.patrolBtn.isEnabled()
    assert not page.patrolCopyBtn.isEnabled(), "没有报告时不该让人复制"


def test_patrol_text_shows_slowest_checks(qapp, hub_root):
    """R229：报告里要有单项耗时榜——13 秒自检慢在哪一眼可见。"""
    w, page = _open_settings(qapp, hub_root)
    rep = _fake_report()
    rep["elapsed_ms"] = 13500
    rep["timings"] = [{"name": "usage_health", "ms": 6100},
                      {"name": "inbox", "ms": 400}]
    txt = page._patrol_text(rep)
    assert "耗时 Top3：usage_health 6.1s · inbox 0.4s" in txt


def test_patrol_text_folds_same_cause(qapp, hub_root):
    """R233：13 项同因跳过要一行说清，别把根因淹在 13 行里。"""
    w, page = _open_settings(qapp, hub_root)
    rep = _fake_report()
    rep.update({"checked": 7, "total": 20, "same_cause_skipped": 13})
    rep["issues"][0].update({"name": "db_integrity", "summary": "大脑库不存在",
                             "skipped_same_cause": 13})
    rep["skipped"] = [{"name": f"check{i}", "why": "大脑库不存在（同因跳过）"}
                      for i in range(13)]

    head = page._patrol_headline(rep)
    txt = page._patrol_text(rep)
    assert "7/20 项" in head and "同因跳过 13" in head, head
    assert "另有 13 项依赖大脑库的检查因同一原因跳过" in txt, txt
    assert "check0、check1、check2、check3 等 13 项" in txt, txt
    assert txt.count("check") == 4, "只列前四个名字，别把 13 行都铺开"


def test_patrol_requires_root(qapp, hub_root, monkeypatch):
    """没设根目录时不该跑（会得到一堆"路径不存在"的假问题）。"""
    import ops
    called = []
    monkeypatch.setattr(ops, "run_patrol",
                        lambda root, progress=None: called.append(root) or _fake_report())
    w, page = _open_settings(qapp, hub_root)
    w.root = ""
    page.patrolBtn.click()
    for _ in range(10):
        qapp.processEvents()
    assert not called, "根目录为空时仍去跑了自检"
    assert not page._patrol_busy
