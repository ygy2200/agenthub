# -*- coding: utf-8 -*-
"""doctor 输出收敛（R178）：摘要只列非零项，修复口径只给失败类别，--full 才展开。"""
import pytest


def _rep():
    return {
        "ok": False, "checked": 20, "passed": ["env"], "elapsed_ms": 12,
        "issues": [{
            "name": "tasks_health",
            "summary": "任务治理：2 项需关注（待办饥饿 2）",
            "fix": "FULL：过期/失联持有者…；待办饥饿：hub_next 认领；死信：hub_task_retry",
            "fix_brief": "待办饥饿：hub_next 认领，或改优先级/关单",
            "detail": ["d1", "d2", "d3", "d4", "d5"],
        }],
        "skipped": [],
    }


def test_doctor_compact_shows_brief_fix_and_three_details(monkeypatch, capsys):
    import cli
    import ops
    monkeypatch.setattr(ops, "run_patrol", lambda root, checks=None: _rep())
    code = cli.cmd_doctor("x")
    out = capsys.readouterr().out
    assert code == 1
    assert "修复：待办饥饿：hub_next 认领" in out
    assert "FULL：" not in out
    assert "d3" in out and "d4" not in out
    assert "另有 2 条明细（加 --full 看全部）" in out


def test_doctor_full_reveals_complete_fix_and_details(monkeypatch, capsys):
    import cli
    import ops
    monkeypatch.setattr(ops, "run_patrol", lambda root, checks=None: _rep())
    code = cli.cmd_doctor("x", full=True)
    out = capsys.readouterr().out
    assert code == 1
    assert "完整口径：FULL" in out
    assert "d5" in out and "--full" not in out


def test_doctor_help_documents_full(monkeypatch, capsys):
    import cli
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor", "--help"])
    assert exc.value.code == 0
    assert "--full" in capsys.readouterr().out


def test_doctor_splits_decisions_from_fixable(monkeypatch, capsys):
    """R218：真实库 5 条红灯里 4 条要人拍板，摘要必须先把两类分开。"""
    import cli
    import ops
    rep = _rep()
    rep["issues"][0]["kind"] = "decide"
    rep["issues"].append({"name": "runtime_health", "summary": "运行版本落后 1 个文件",
                          "fix": "重启 MCP 进程", "kind": "fix", "detail": []})
    rep["decide_count"], rep["fix_count"] = 1, 1
    monkeypatch.setattr(ops, "run_patrol", lambda root, checks=None: rep)
    assert cli.cmd_doctor("x") == 1
    out = capsys.readouterr().out
    assert "问题 2（可直接修复 1 · 需你拍板 1）" in out
    assert "✗ tasks_health（要拍板）：" in out
    assert "✗ runtime_health：" in out and "runtime_health（要拍板）" not in out
    assert "不是照着修就能消的" in out


def test_doctor_all_decisions_wording(monkeypatch, capsys):
    import cli
    import ops
    rep = _rep()
    rep["issues"][0]["kind"] = "decide"
    rep["decide_count"], rep["fix_count"] = 1, 0
    monkeypatch.setattr(ops, "run_patrol", lambda root, checks=None: rep)
    cli.cmd_doctor("x")
    out = capsys.readouterr().out
    assert "问题 1（均需你拍板 1）" in out
    assert "可直接执行" not in out


def test_doctor_marks_machine_scope(monkeypatch, capsys):
    """R228：机器级问题（运行进程/全局配置）必须标注，别让人以为新库坏了。"""
    import cli
    import ops
    rep = _rep()
    rep["issues"] = [
        {"name": "runtime_health", "summary": "运行版本落后 4 个文件",
         "fix": "重启 MCP", "kind": "fix", "scope": "machine", "detail": []},
        {"name": "rules_health", "summary": "规则执行缺口：缺配置 codex",
         "fix": "onboard", "kind": "decide", "scope": "machine", "detail": []},
    ]
    rep["decide_count"], rep["fix_count"] = 1, 1
    monkeypatch.setattr(ops, "run_patrol", lambda root, checks=None: rep)
    assert cli.cmd_doctor("x") == 1
    out = capsys.readouterr().out
    assert "✗ runtime_health（机器级）：" in out
    assert "✗ rules_health（机器级 · 要拍板）：" in out
    assert "2 项是机器级状态" in out and "与当前根目录无关" in out


def test_doctor_shows_slowest_checks(monkeypatch, capsys):
    """R229：13 秒自检要能回答「慢在哪」——总耗时上 3s 时给最慢三项。"""
    import cli
    import ops
    rep = _rep()
    rep["elapsed_ms"] = 13500
    rep["timings"] = [{"name": "usage_health", "ms": 6100},
                      {"name": "stalled", "ms": 2200},
                      {"name": "inbox", "ms": 900},
                      {"name": "env", "ms": 100}]
    monkeypatch.setattr(ops, "run_patrol", lambda root, checks=None: rep)
    assert cli.cmd_doctor("x") == 1
    out = capsys.readouterr().out
    # R237 起统一成「ms + 占实跑项比例」一种写法（`--slow N` 与这里的自动账本同格式，
    # 免得更慢的一项用秒、更快的用毫秒让人心算），Top3 的排序与「只点最慢三项」的原意不变
    assert "耗时 Top3：usage_health 6100ms(66%) · stalled 2200ms(24%) · inbox 900ms(10%)" in out
    assert "env" not in out.split("耗时 Top3：")[1].splitlines()[0]


def test_doctor_folds_same_cause_skips(monkeypatch, capsys):
    """R233：空目录 13 项同因跳过要一次说清，不再 13 行各报一份修复手册。"""
    import cli
    import ops
    rep = {
        "ok": False, "checked": 7, "total": 20, "same_cause_skipped": 13,
        "passed": ["env", "backup_health", "mcp_drift"], "elapsed_ms": 10,
        "issues": [{"name": "db_integrity", "summary": "大脑库不存在",
                    "fix": "FULL", "fix_brief": "建库", "kind": "fix",
                    "detail": [], "skipped_same_cause": 13}],
        "skipped": [{"name": n, "why": "大脑库不存在（同因跳过）"}
                    for n in ("db_health", "audit_chain", "security_health",
                              "usage_health", "stale_health", "ghost_memory",
                              "dup_mem", "record_quality", "date_health",
                              "tasks_health", "governance_health", "lease_health",
                              "stalled")],
    }
    monkeypatch.setattr(ops, "run_patrol", lambda root, checks=None: rep)
    assert cli.cmd_doctor("x") == 1
    out = capsys.readouterr().out
    assert "一键自检：7/20 项（同因跳过 13），通过 3，问题 1" in out
    assert "另有 13 项依赖大脑库的检查因同一原因跳过" in out
    assert "db_health、audit_chain、security_health、usage_health 等 13 项" in out
    assert "✗ db_health" not in out


def test_doctor_fast_run_has_no_timing_line(monkeypatch, capsys):
    import cli
    import ops
    rep = _rep()
    rep["timings"] = [{"name": "env", "ms": 4}]
    monkeypatch.setattr(ops, "run_patrol", lambda root, checks=None: rep)
    cli.cmd_doctor("x")
    assert "耗时 Top3" not in capsys.readouterr().out


def test_run_patrol_tags_decision_checks(monkeypatch):
    import ops
    monkeypatch.setattr(ops, "run_check",
                        lambda name, target="", root="": {"ok": False, "summary": "x",
                                                          "detail": []})
    rep = ops.run_patrol("x", checks=["usage_health", "db_health"])
    kinds = {i["name"]: i["kind"] for i in rep["issues"]}
    assert kinds == {"usage_health": "decide", "db_health": "fix"}
    assert (rep["decide_count"], rep["fix_count"]) == (1, 1)
    assert [t["name"] for t in rep["timings"]] == ["usage_health", "db_health"]
    assert all(t["ms"] >= 0 for t in rep["timings"])


def test_tasks_health_summary_lists_only_nonzero(monkeypatch, hub_root):
    import brain
    import ops
    brain.init_db(hub_root)
    _, tid = brain.task_add(hub_root, "排队没人做的活", project="甲-项目")
    monkeypatch.setattr(brain, "task_starvation",
                        lambda root, limit=1000: {"starving": [
                            {"id": tid, "_age_days": 5.1}]})
    monkeypatch.setattr(brain, "task_graph_audit", lambda root: {
        "dangling": [], "residual_blocked": [], "cycles": [],
        "counts": {"dangling": 0, "cycles": 0, "residual_blocked": 0}})
    monkeypatch.setattr(brain, "task_closure_drift",
                        lambda root, days=30, limit=100: {
                            "items": [], "counts": {"total": 0}})

    r = ops.run_check("tasks_health", "", hub_root)
    assert r["summary"] == "任务治理：1 项需关注（待办饥饿 1）"
    assert "过期租约 0" not in r["summary"]
    assert "hub_next" in r["fix"]
    assert "死信" not in r["fix"], "修复口径不应包含未失败的类别"
    # 完整口径仍保留在 CHECK_FIXES，doctor --full 可回退查看
    assert "死信" in ops.CHECK_FIXES["tasks_health"]
