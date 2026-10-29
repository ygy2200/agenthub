# -*- coding: utf-8 -*-
"""R702：onboard 前先确保 MCP server 已部署，别把接入条目写到不存在的路径。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _adapter(tmp_path):
    return {"agent": "x", "instr_path": str(tmp_path / "instr.md"),
            "mcp": {"path": str(tmp_path / "mcp.json"),
                    "layout": "standard", "format": "json"}}


def _wire(monkeypatch, cli, tmp_path, state, deploy_result, seen):
    a = _adapter(tmp_path)
    monkeypatch.setattr(cli.core, "find_adapter", lambda root, agent: a)
    monkeypatch.setattr(cli.core, "_mcp_spec_of",
                        lambda ad: (ad["mcp"]["path"], "json", ["mcpServers"]))
    monkeypatch.setattr(cli.core, "deploy_status",
                        lambda: {"state": state, "server": "", "python": "py",
                                 "diff": [], "absent": [], "same": 0})
    monkeypatch.setattr(cli.core, "deploy_server", lambda: deploy_result)

    def fake_install(path, layout, python_exe, server_py, root, **kw):
        seen["server"] = server_py
        return ""

    monkeypatch.setattr(cli.core, "install_mcp_entry", fake_install)
    monkeypatch.setattr(cli.core, "inject_bootstrap", lambda *args, **kw: "")


def test_onboard_deploys_missing_server_first(tmp_path, monkeypatch, capsys):
    import cli

    seen = {}
    target = str(tmp_path / "deployed" / "agenthub_mcp.py")
    _wire(monkeypatch, cli, tmp_path, "missing", ("py", target), seen)
    rc = cli.cmd_onboard(str(tmp_path), "x")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "MCP server 已部署" in out
    assert seen["server"] == target, seen


def test_onboard_aborts_when_deploy_fails(tmp_path, monkeypatch, capsys):
    import cli

    seen = {}
    _wire(monkeypatch, cli, tmp_path, "missing", ("py", ""), seen)
    rc = cli.cmd_onboard(str(tmp_path), "x")
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "接入未完成" in out and "deploy" in out
    assert seen == {}, "部署失败后不应写接入配置"
