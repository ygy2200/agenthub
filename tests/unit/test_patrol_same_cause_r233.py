# -*- coding: utf-8 -*-
"""巡检同因折叠（R233）。

空目录实测：20 项巡检产出 17 条问题，其中 14 条是「大脑库不存在」的同一个根因
（4 条走 _db_missing，10 条是裸 sqlite `unable to open database file`），每条还各带
一段与病因无关的修复手册。用户看到的是一屏红，真正该做的一件事（建库/指对根目录）
反而被淹掉。本文件守：
  ① 库里不存在只报一条，后续依赖库的检查跳过并计入 same_cause_skipped；
  ② 报告里不再出现裸 sqlite 错误文案；
  ③ 同一 cause 的重复问题折叠进首条（also），不各占一节；
  ④ 健康库一项不漏、不误折叠。
"""
import ops


def _cause_fail(name, summary="大脑库不存在", fix="x"):
    return {"ok": False, "summary": summary, "detail": [],
            "cause": "db_missing", "fix": fix}


def test_empty_root_collapses_to_single_db_issue(hub_root):
    rep = ops.run_patrol(hub_root)

    assert rep["total"] == len(ops.PATROL_CHECKS) == 21
    assert rep["same_cause_skipped"] == len(ops.DB_BACKED_CHECKS) - 1 == 13

    db_issues = [i["name"] for i in rep["issues"] if i["name"] in ops.DB_BACKED_CHECKS]
    assert db_issues == ["db_integrity"], f"同因库检查应只剩一条：{db_issues}"

    blob = " ".join(
        str(i.get("summary", "")) + " " + " ".join(str(d) for d in i.get("detail") or [])
        for i in rep["issues"])
    assert "unable to open database file" not in blob, "裸 sqlite 错误文案不该出现在问题里"

    primary = next(i for i in rep["issues"] if i["name"] == "db_integrity")
    assert primary["skipped_same_cause"] == 13
    assert primary.get("fix_brief"), "根因条目仍要带可执行修复口径"

    skipped = {s["name"]: s["why"] for s in rep["skipped"]}
    assert set(skipped) == ops.DB_BACKED_CHECKS - {"db_integrity"}
    assert all("同因" in why for why in skipped.values()), skipped
    assert rep["checked"] + len(rep["skipped"]) == rep["total"]


def test_custom_order_first_db_check_becomes_primary(monkeypatch):
    calls = []

    def fake(name, target="", root=""):
        calls.append(name)
        if name in ops.DB_BACKED_CHECKS:
            return _cause_fail(name)
        return {"ok": True, "summary": "ok", "detail": []}

    monkeypatch.setattr(ops, "run_check", fake)
    rep = ops.run_patrol("x", checks=["db_health", "db_integrity", "audit_chain", "env"])

    assert [i["name"] for i in rep["issues"]] == ["db_health"]
    assert rep["issues"][0]["skipped_same_cause"] == 2
    assert [s["name"] for s in rep["skipped"]] == ["db_integrity", "audit_chain"]
    assert rep["same_cause_skipped"] == 2
    assert calls == ["db_health", "env"], "根因确认后不该再跑依赖库的检查"
    assert [t["name"] for t in rep["timings"]] == ["db_health", "env"]


def test_repeated_cause_folds_into_first_issue(monkeypatch):
    def fake(name, target="", root=""):
        if name == "db_integrity":
            return _cause_fail(name)
        if name == "env":
            return _cause_fail(name, summary="env 读库失败")
        return {"ok": True, "summary": "ok", "detail": []}

    monkeypatch.setattr(ops, "run_check", fake)
    rep = ops.run_patrol("x", checks=["db_integrity", "env"])

    assert [i["name"] for i in rep["issues"]] == ["db_integrity"]
    assert rep["issues"][0]["also"] == ["env"]
    assert rep["same_cause_skipped"] == 0


def test_healthy_root_keeps_all_db_checks(hub_root):
    import brain

    brain.init_db(hub_root)
    rep = ops.run_patrol(hub_root, checks=["db_integrity", "db_health",
                                           "audit_chain", "security_health"])
    assert rep["ok"] is True, rep["issues"]
    assert rep["total"] == 4 and rep["checked"] == 4
    assert rep["same_cause_skipped"] == 0
    assert not rep["skipped"], rep["skipped"]


def test_db_backed_registry_covers_patrol_db_checks():
    non_db = set(ops.PATROL_CHECKS) - ops.DB_BACKED_CHECKS
    assert non_db == {"env", "runtime_health", "backup_health",
                      "rules_health", "mcp_drift", "inbox", "brief_hook_health"}, non_db
    assert ops.DB_BACKED_CHECKS <= set(ops.CHECKS)


def test_format_skipped_groups_by_reason():
    same = [{"name": f"c{i}", "why": "大脑库不存在（同因跳过）"} for i in range(13)]
    lines = ops.format_skipped(same)
    assert len(lines) == 1 and "等 13 项" in lines[0] and "c0" in lines[0]

    mixed = [{"name": "a", "why": "原因甲"}, {"name": "b", "why": "原因乙"},
             {"name": "c", "why": "原因甲"}]
    lines = ops.format_skipped(mixed)
    assert len(lines) == 2
    assert "a（原因甲）、c（原因甲）" in lines[0]
    assert lines[1] == "b（原因乙）"
