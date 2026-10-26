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
    monkeypatch.setattr(ops, "run_patrol", lambda root: _rep())
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
    monkeypatch.setattr(ops, "run_patrol", lambda root: _rep())
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
