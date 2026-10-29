# -*- coding: utf-8 -*-
"""R700：minutes=0 是「持续冻结直到手动解除」，旧文案「0=手动解冻」会让人以为立即解冻。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def test_freeze_zero_means_persistent_freeze(tmp_path):
    import brain

    root = str(tmp_path / "hub")
    Path(root).mkdir()
    brain.init_db(root)
    err, fr = brain.freeze_set(root, 0, "迁移窗口", "qoder")
    assert not err and fr["active"] and not fr["until"], fr


def test_help_and_schema_use_unambiguous_wording(capsys):
    import cli

    with pytest.raises(SystemExit):
        cli.main(["freeze", "--help"])
    out = capsys.readouterr().out
    assert "0=持续冻结直到 --off" in out
    assert "0=手动解冻" not in out

    import agenthub_mcp as m

    tool = [t for t in m.TOOLS if t["name"] == "hub_freeze"][0]
    minutes = tool["inputSchema"]["properties"]["minutes"]
    assert "持续冻结" in minutes["description"]
    assert "0=手动解冻" not in tool["description"]


def test_mcp_freeze_zero_reports_persistent(tmp_path):
    import agenthub_mcp as m
    import brain

    root = str(tmp_path / "hub")
    Path(root).mkdir()
    brain.init_db(root)
    out = m.call_tool("hub_freeze", {"action": "freeze", "minutes": 0,
                                     "reason": "迁移"}, root)
    assert "持续冻结" in out and "手动解除" in out, out
