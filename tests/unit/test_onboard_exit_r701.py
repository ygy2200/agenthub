# -*- coding: utf-8 -*-
"""R701：onboard 有写入失败时必须非零退出，且不能打印「接入完成」。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _adapter(tmp_path, with_boot=True):
    a = {"agent": "x", "mcp": {"path": str(tmp_path / "mcp.json"),
                               "layout": "standard", "format": "json"}}
    if with_boot:
        a["instr_path"] = str(tmp_path / "instr.md")
    return a


def _ready(monkeypatch, cli):
    monkeypatch.setattr(cli.core, "deploy_status",
                        lambda: {"state": "ready", "server": "s", "python": "py",
                                 "diff": [], "absent": [], "same": 11})


def test_onboard_write_failure_returns_nonzero(tmp_path, monkeypatch, capsys):
    import cli

    _ready(monkeypatch, cli)
    a = _adapter(tmp_path)
    monkeypatch.setattr(cli.core, "find_adapter", lambda root, agent: a)
    monkeypatch.setattr(cli.core, "_mcp_spec_of",
                        lambda ad: (ad["mcp"]["path"], "json", ["mcpServers"]))
    monkeypatch.setattr(cli.core, "install_mcp_entry", lambda *args, **kw: "磁盘只读")
    monkeypatch.setattr(cli.core, "inject_bootstrap", lambda *args, **kw: "")

    rc = cli.cmd_onboard(str(tmp_path), "x")
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "接入完成" not in out and "接入未完成" in out and "磁盘只读" in out


def test_onboard_partial_success_is_failure(tmp_path, monkeypatch, capsys):
    import cli

    _ready(monkeypatch, cli)
    a = _adapter(tmp_path)
    monkeypatch.setattr(cli.core, "find_adapter", lambda root, agent: a)
    monkeypatch.setattr(cli.core, "_mcp_spec_of",
                        lambda ad: (ad["mcp"]["path"], "json", ["mcpServers"]))
    monkeypatch.setattr(cli.core, "install_mcp_entry", lambda *args, **kw: "")
    monkeypatch.setattr(cli.core, "inject_bootstrap", lambda *args, **kw: "文件被占用")

    rc = cli.cmd_onboard(str(tmp_path), "x")
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "MCP 配置" in out and "文件被占用" in out and "接入完成" not in out


def test_onboard_without_any_endpoint_is_failure(tmp_path, monkeypatch, capsys):
    import cli

    monkeypatch.setattr(cli.core, "find_adapter", lambda root, agent: {"agent": "x"})
    rc = cli.cmd_onboard(str(tmp_path), "x")
    out = capsys.readouterr().out
    assert rc == 1 and "没有可写入的接入点" in out, out
