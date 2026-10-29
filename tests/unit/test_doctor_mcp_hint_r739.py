# -*- coding: utf-8 -*-
"""R739：doctor 的「修复」行给的是 MCP 工具名。

真实库实测：
    ✗ backup_health：备份健康：可清理 1374MB
      修复：hub_export action=backup 生成整包；清理用 action=prune（确认后 apply=true）
    ✗ ghost_memory：修复：先 hub_memory_read / hub_get_record 核对…hub_memory_supersede(keep_id, drop_id)
    ✗ tasks_health：修复：待办饥饿：hub_next 认领，或改优先级/关单
`hub_export` 在命令行是 command not found；`hub_memory_supersede(keep_id, drop_id)` 更离谱——
那是 Python 调用写法，不是命令也不是工具参数。

注册表文案不能改（MCP 面 `hub_ops_run` 读它是对的），所以在 doctor 收尾补一行换算；
有 CLI 原生命令的指原生命令，其余教 `call`，且不把十个名字堆一行。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402
import ops as ops_mod  # noqa: E402


def _rep(issues):
    return {"ok": not issues, "issues": issues, "checked": 1, "total": 1,
            "passed": [], "skipped": [], "same_cause_skipped": 0,
            "decide_count": 0, "fix_count": len(issues), "timings": [],
            "elapsed_ms": 1}


def _issue(fix: str, name="x"):
    return {"name": name, "summary": "s", "fix": fix, "kind": "fix",
            "detail": [], "scope": "root"}


def test_no_tools_no_hint():
    assert cli._mcp_tool_hint([_issue("把目录指对再跑")]) == ""


def test_native_commands_take_precedence():
    line = cli._mcp_tool_hint([_issue("hub_claim 认领；hub_next 看下一步")])
    assert f"{cli.CMD} claim" in line and f"{cli.CMD} next" in line
    assert "call hub_claim" not in line  # 有原生命令就别教 call


def test_call_only_tools_are_grouped_and_capped():
    line = cli._mcp_tool_hint([_issue(
        "hub_export action=backup；hub_memory_read；hub_get_record；"
        "hub_memory_supersede(keep_id, drop_id)；hub_usage_timeline")])
    assert f"{cli.CMD} call <工具名>" in line
    assert "hub_export" in line
    assert "等 5 个" in line          # 多于 3 个要折叠，不能堆成长串
    assert line.count("、") <= 2       # 只列前 3 个
    assert len(line) < 160


def test_doctor_prints_hint_when_fix_mentions_tool(monkeypatch, capsys):
    monkeypatch.setattr(ops_mod, "run_patrol", lambda root, checks: _rep(
        [_issue("hub_export action=backup 生成整包", "backup_health")]))
    assert cli.main(["doctor", "--no-machine"]) == 1
    out = capsys.readouterr().out
    assert "MCP 工具名" in out and f"{cli.CMD} call <工具名>" in out


def test_doctor_quiet_when_no_tool_names(monkeypatch, capsys):
    monkeypatch.setattr(ops_mod, "run_patrol", lambda root, checks: _rep(
        [_issue("python cli.py offboard <agent> --apply 下线接入", "usage_health")]))
    assert cli.main(["doctor", "--no-machine"]) == 1
    assert "MCP 工具名" not in capsys.readouterr().out
