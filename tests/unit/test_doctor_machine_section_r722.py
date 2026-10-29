# -*- coding: utf-8 -*-
"""R722：doctor 把机器级问题单列一节。

修之前会怎样失败：根目录问题和机器级问题（运行进程里的旧代码、别的 agent 的配置）共用
同一串 ✗，只有行尾「（机器级）」三个字区分——两行连在一起看，像「同一个地方坏了两处」，
而修法完全不同：一个要建库/指对根目录，一个要重启 MCP 进程，跟当前 --root 毫无关系。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ROOT_ISSUE = {"name": "db_integrity", "summary": "大脑库不存在", "fix": "FULL",
              "fix_brief": "建库", "kind": "fix", "detail": []}
MACHINE_ISSUE = {"name": "runtime_health", "summary": "运行版本落后", "fix": "重启 MCP",
                 "fix_brief": "重启 MCP", "kind": "fix", "scope": "machine", "detail": []}


def _rep(issues):
    return {"ok": not issues, "checked": 20, "total": 20,
            "passed": [], "elapsed_ms": 5, "issues": issues, "skipped": [],
            "decide_count": sum(1 for i in issues if i.get("kind") == "decide"),
            "fix_count": sum(1 for i in issues if i.get("kind") != "decide"),
            "timings": []}


def _run(monkeypatch, capsys, issues, **kw):
    import cli
    import ops

    def fake(root, checks=None):
        # 照实遵守 checks：--no-machine 是在入口把机器级项从清单里摘掉，
        # 假巡检若不认这个参数就测不到「分节消失」这条路径
        kept = [i for i in issues if checks is None or i["name"] in checks]
        return _rep(kept)

    monkeypatch.setattr(ops, "run_patrol", fake)
    code = cli.cmd_doctor("x", **kw)
    return code, capsys.readouterr().out


def test_mixed_issues_split_into_two_sections(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, [dict(ROOT_ISSUE), dict(MACHINE_ISSUE)])
    assert code == 1
    assert "问题 2（根目录 1 · 机器级 1）" in out
    assert "机器级问题（与本目录无关" in out
    # 顺序：根目录问题在前（用户改得了的），机器级在后
    assert out.index("✗ db_integrity") < out.index("机器级问题") < out.index("✗ runtime_health")


def test_machine_issue_inside_section_drops_redundant_tag(monkeypatch, capsys):
    """分节标题已经说过「机器级」，节内再贴一次「（机器级）」是重复标注。"""
    _, out = _run(monkeypatch, capsys, [dict(ROOT_ISSUE), dict(MACHINE_ISSUE)])
    assert "✗ runtime_health：" in out
    assert "（机器级）" not in out


def test_only_machine_issues_does_not_open_root_section(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, [dict(MACHINE_ISSUE)])
    assert code == 1
    assert "问题 1（机器级 1）" in out
    assert "机器级问题（与本目录无关" in out
    assert "✗ db_integrity" not in out


def test_no_machine_issues_keeps_old_layout(monkeypatch, capsys):
    """没机器级问题时一字不改老版式——不能为了分节给干净结果加空标题。"""
    _, out = _run(monkeypatch, capsys, [dict(ROOT_ISSUE)])
    assert "问题 1" in out
    assert "机器级" not in out
    assert "✗ db_integrity" in out


def test_no_machine_flag_removes_the_section(monkeypatch, capsys):
    _, out = _run(monkeypatch, capsys, [dict(ROOT_ISSUE), dict(MACHINE_ISSUE)],
                  no_machine=True)
    assert "机器级" not in out
