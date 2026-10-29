# -*- coding: utf-8 -*-
r"""清单类工具饱和口径第二批（R273）。

待审批队列/检查点时间线/隔离区/文件租约都是「LIMIT 后拿 len 当总数」，
资产检索与蒸馏候选取满上限时也不说还有；使用时间线截断不说还有更早。
守：①超限报「共 N 条，这里列 M 条」；②未超限不出现该措辞；
③取满上限的检索报 ≥N；④截断带省略号。
"""
import agenthub_mcp as m
import brain


def _approval_tasks(root, n):
    with brain.db_conn(root) as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO tasks(project,title,status,owner,claimed_at,approval_required) "
                "VALUES(?,?,'in_progress','qa','2026-10-01 00:00:00',1)",
                (f"P-{i % 3}", f"待批任务{i}"))


def test_approvals_over_limit_reports_total(hub_root):
    brain.init_db(hub_root)
    _approval_tasks(hub_root, 55)
    out = m.call_tool("hub_tasks", {"view": "approvals"}, hub_root)
    assert "待审批任务 共 55 条，这里列 50 条" in out, out[:300]


def test_approvals_under_limit_no_total_phrase(hub_root):
    brain.init_db(hub_root)
    _approval_tasks(hub_root, 2)
    out = m.call_tool("hub_tasks", {"view": "approvals"}, hub_root)
    assert "待审批任务 2 条" in out, out[:300]
    assert "这里列" not in out


def test_checkpoint_over_limit_reports_total(hub_root):
    brain.init_db(hub_root)
    _, tid = brain.task_add(hub_root, "长任务")
    with brain.db_conn(hub_root) as conn:
        for i in range(25):
            conn.execute(
                "INSERT INTO checkpoints(task_id,agent,ts,kind,content) VALUES(?,?,?,?,?)",
                (tid, "qa", f"2026-10-01 00:{i:02d}:00", "manual", "步骤" * 50 + str(i)))
    out = m.call_tool("hub_checkpoint", {"action": "list", "task_id": tid, "limit": 10},
                      hub_root)
    assert "检查点时间线 共 25 条，这里列 10 条" in out, out[:300]
    assert "…" in out, out[:400]
    out_all = m.call_tool("hub_checkpoint", {"action": "list", "task_id": tid, "limit": 25},
                          hub_root)
    assert "这里列" not in out_all


def test_quarantine_over_limit_reports_total(hub_root):
    brain.init_db(hub_root)
    for i in range(12):
        brain.add_record(hub_root, "P-项目", "qa", "2026-10-01",
                         "可疑" + "长" * 80 + str(i), "正文" * 60)
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE records SET status='quarantine'")
    out = m.call_tool("hub_quarantine", {"limit": 5}, hub_root)
    assert "隔离区 共 12 条，这里列 5 条" in out, out[:300]
    assert "…" in out, out[:400]


def test_lease_over_limit_reports_total(hub_root):
    brain.init_db(hub_root)
    with brain.db_conn(hub_root) as conn:
        for i in range(55):
            conn.execute(
                "INSERT INTO file_leases(path,display_path,project,owner,reason,expires_at) "
                "VALUES(?,?,?,?,?,?)",
                (f"d:/hub/f{i}.py", f"f{i}.py", "P-项目", "qa", "改" * 50,
                 "2099-01-01 00:00:00"))
    out = m.call_tool("hub_lock", {"action": "list"}, hub_root)
    assert "文件租约 共 55 条，这里列 50 条" in out, out[:300]
    assert "…" in out, out[:400]


def test_asset_find_saturation(hub_root, monkeypatch):
    brain.init_db(hub_root)
    monkeypatch.setattr(m.brain, "asset_find", lambda root, kw, limit=8: [
        {"name": f"软件{i}", "category": "工具", "license": "", "desc": "说明" * 50,
         "website": ""} for i in range(limit)])
    out = m.call_tool("hub_asset_find", {"keyword": "录屏"}, hub_root)
    assert "≥8 条" in out and "已达 limit=8" in out, out[:300]
    assert "…" in out, out[:400]


def test_distill_saturation(hub_root, monkeypatch):
    brain.init_db(hub_root)
    monkeypatch.setattr(m.brain, "distill_candidates", lambda root, limit=15: [
        {"id": i, "date": "2026-10-01", "agent": "qa", "project": "P-项目",
         "gist": "目的" * 80, "meta": False} for i in range(1, 16)])
    monkeypatch.setattr(m.brain, "mark_distill_shown", lambda *a, **k: None)
    out = m.call_tool("hub_distill", {}, hub_root)
    assert "≥15 条" in out and "已达 limit 上限" in out, out[:300]
    assert "…" in out, out[:500]


def test_usage_timeline_truncation_note(hub_root, monkeypatch):
    brain.init_db(hub_root)
    rows = [{"ts": f"2026-10-01T00:{i:02d}:00", "source": "call", "agent": "qa",
             "subject": f"hub_search #{i}", "detail": ""} for i in range(51)]
    monkeypatch.setattr(m.brain, "usage_timeline",
                        lambda root, days=7, agent="", tool="", limit=50: rows[:limit])
    out = m.call_tool("hub_usage_timeline", {}, hub_root)
    assert "还有更早事件" in out, out[:300]
    assert out.count("hub_search #") == 50, out.count("hub_search #")
