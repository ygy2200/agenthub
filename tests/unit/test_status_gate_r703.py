# -*- coding: utf-8 -*-
"""R703：status 有缺失时退 1，并可输出 JSON 供 CI/装机脚本消费。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _row(agent, mcp=True, boot=True, stale=False):
    return {"agent": agent, "channel": "instr_file", "instr_path": "x",
            "boot": boot, "boot_version": "6", "boot_stale": stale,
            "mcp_path": "", "layout": "", "profile": "", "mcp": mcp}


def test_status_exit_code_reflects_problems(tmp_path, capsys, monkeypatch):
    import cli
    import core

    monkeypatch.setattr(core, "load_config", lambda: {})
    monkeypatch.setattr(core, "agent_bootstrap_status",
                        lambda root: [_row("ok"), _row("broken", mcp=False)])
    rc = cli.main(["status", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "1 个 agent 需要处理" in out and "onboard" in out


def test_status_json_is_machine_readable(tmp_path, capsys, monkeypatch):
    import cli
    import core

    monkeypatch.setattr(core, "load_config", lambda: {})
    monkeypatch.setattr(core, "agent_bootstrap_status",
                        lambda root: [_row("ok"), _row("stale", stale=True)])
    rc = cli.main(["status", "--root", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 1, out
    data = json.loads(out)
    assert data["healthy"] is False
    assert data["problems"] == [{"agent": "stale", "issues": ["引导过期"]}]
    assert [a["agent"] for a in data["agents"]] == ["ok", "stale"]


def test_status_healthy_returns_zero(tmp_path, capsys, monkeypatch):
    import cli
    import core

    monkeypatch.setattr(core, "load_config", lambda: {})
    monkeypatch.setattr(core, "agent_bootstrap_status", lambda root: [_row("ok")])
    rc = cli.main(["status", "--root", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0 and json.loads(out)["healthy"] is True, out
