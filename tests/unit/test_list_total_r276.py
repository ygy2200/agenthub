# -*- coding: utf-8 -*-
r"""列表总数口径第三批（R276）：审计 / 完成质量巡检 / 流程执行历史。

三个入口都会在 LIMIT 之后报「N 条」，但 N 只是取到的条数：
- hub_audit 超过 limit 就看不到更早的写操作；
- hub_task_quality 的 counts.total 实际是「体检了几单」，窗口内总数更大时无提示；
- hub_playbook action=runs 超过 limit 就谎报执行历史总量。
这里统一成「共 N 条，这里列 M 条」，未超限保持原文案。
"""
import brain
import agenthub_mcp as m


def _seed_audits(root, n):
    brain.init_db(root)
    for i in range(n):
        brain.audit_add(root, "qa", "hub_log_work", summary=f"审计 {i}")


def _seed_done_tasks(root, n, project=""):
    brain.init_db(root)
    tids = []
    for i in range(n):
        err, tid = brain.task_add(root, f"质检 {i}", project=project)
        assert not err, err
        err, _ = brain.task_update(root, tid, status="done")
        assert not err, err
        tids.append(tid)
    return tids


def _seed_runs(root, n, playbook="pb-a"):
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO playbook_runs(playbook,project,agent,status,started,summary)"
                " VALUES(?,?,?,?,?,?)",
                (playbook, "proj", "qa", "done", "2026-10-29T10:00:00", f"run {i}"))


def test_audit_reports_total(hub_root):
    _seed_audits(hub_root, 25)
    out = m.call_tool("hub_audit", {"limit": 10}, hub_root)
    assert "共 25 条，这里列 10 条" in out, out[:300]


def test_audit_under_limit_keeps_old_head(hub_root):
    _seed_audits(hub_root, 3)
    out = m.call_tool("hub_audit", {"limit": 20}, hub_root)
    assert "最近 3 条写操作审计" in out, out[:300]
    assert "这里列" not in out


def test_quality_reports_matched_total(hub_root):
    _seed_done_tasks(hub_root, 12)
    rep = brain.task_closure_quality_report(hub_root, limit=5)
    assert rep["matched"] == 12 and rep["counts"]["total"] == 5, rep["counts"]
    out = m.call_tool("hub_task_quality", {"limit": 5}, hub_root)
    assert "窗口内 12 单，本次体检 5 单" in out, out[:400]


def test_quality_under_limit_has_no_truncation_wording(hub_root):
    _seed_done_tasks(hub_root, 3)
    out = m.call_tool("hub_task_quality", {"limit": 20}, hub_root)
    assert "本次体检" not in out, out[:400]
    assert "3 单" in out.splitlines()[0], out[:400]


def test_quality_matched_respects_project_filter(hub_root):
    _seed_done_tasks(hub_root, 4, project="项目甲")
    _seed_done_tasks(hub_root, 6, project="项目乙")
    rep = brain.task_closure_quality_report(hub_root, project="项目甲", limit=2)
    assert rep["matched"] == 4 and rep["counts"]["total"] == 2, rep["counts"]


def test_playbook_runs_reports_total(hub_root):
    _seed_runs(hub_root, 15)
    out = m.call_tool("hub_playbook", {"action": "runs", "limit": 5}, hub_root)
    assert "共 15 条，这里列 5 条" in out, out[:400]


def test_playbook_runs_under_limit_keeps_old_head(hub_root):
    _seed_runs(hub_root, 3)
    out = m.call_tool("hub_playbook", {"action": "runs", "limit": 10}, hub_root)
    assert "执行历史 3 条（新在前）" in out, out[:400]
    assert "这里列" not in out


def test_playbook_runs_total_respects_name_filter(hub_root):
    _seed_runs(hub_root, 4, "pb-a")
    _seed_runs(hub_root, 6, "pb-b")
    out = m.call_tool("hub_playbook", {"action": "runs", "name": "pb-a", "limit": 2}, hub_root)
    assert "共 4 条，这里列 2 条" in out, out[:400]
