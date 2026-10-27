# -*- coding: utf-8 -*-
"""R206：cli.py offboard 下线接入（默认预演，--apply 才写；两处都先备份）。"""
import json

import cli
import core


def _setup(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    instr = tmp_path / "AGENTS.md"
    mcp = tmp_path / "mcp.json"
    (root / "config.agents.json").write_text(json.dumps({"agents": [{
        "agent": "fake", "channel": "instr_file", "instr_path": str(instr),
        "mcp": {"path": str(mcp), "layout": "standard"}}]}), encoding="utf-8")
    assert core.inject_bootstrap(str(instr), str(root), agent="fake") == ""
    assert core.install_mcp_entry(str(mcp), "standard", "python", "server.py",
                                  str(root), agent="fake") == ""
    return root, instr, mcp


def test_offboard_dry_run_then_apply(tmp_path):
    root, instr, mcp = _setup(tmp_path)
    assert cli.cmd_offboard(str(root), "fake") == 0
    assert core.BOOTSTRAP_BEGIN in instr.read_text(encoding="utf-8")
    assert core.mcp_has_agenthub(str(mcp), "standard")

    assert cli.cmd_offboard(str(root), "fake", apply=True) == 0
    assert core.BOOTSTRAP_BEGIN not in instr.read_text(encoding="utf-8")
    assert not core.mcp_has_agenthub(str(mcp), "standard")
    data = json.loads(mcp.read_text(encoding="utf-8"))
    assert data.get("mcpServers", {}).get("agenthub") is None


def test_offboard_keep_one_side(tmp_path):
    root, instr, mcp = _setup(tmp_path)
    assert cli.cmd_offboard(str(root), "fake", apply=True, keep_mcp=True) == 0
    assert core.BOOTSTRAP_BEGIN not in instr.read_text(encoding="utf-8")
    assert core.mcp_has_agenthub(str(mcp), "standard")

    assert cli.cmd_offboard(str(root), "fake", apply=True) == 0
    assert not core.mcp_has_agenthub(str(mcp), "standard")


def test_offboard_unknown_agent(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert cli.cmd_offboard(str(root), "nobody") == 1
