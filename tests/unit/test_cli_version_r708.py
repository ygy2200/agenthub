# -*- coding: utf-8 -*-
"""R708/R709：`agenthub --version` 与未知命令的可读提示。

实测缺口（R700 系列收尾走查）：
  · `agenthub --version` → `unrecognized arguments: --version`（退 2），排障第一问没有出口；
  · `agenthub statu` → argparse 甩一屏「invalid choice: 'statu'（34 个选项全列）」，没有纠错建议。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402
import agenthub_mcp  # noqa: E402


def test_version_flag_any_position(capsys):
    for argv in (["--version"], ["-V"], ["status", "--version"]):
        capsys.readouterr()
        assert cli.main(argv) == 0
        out = capsys.readouterr().out
        assert agenthub_mcp.SERVER_INFO["version"] in out
        assert out.startswith("AgentHub v")


def test_version_line_carries_protocol_and_python():
    line = cli._version_line()
    assert "MCP 协议" in line and agenthub_mcp.PROTOCOL_VERSION in line
    assert f"Python {sys.version.split()[0]}" in line


def test_unknown_command_suggests_nearest(capsys):
    assert cli.main(["statu"]) == 2
    out = capsys.readouterr().out
    assert "未知命令：statu" in out
    assert "你可能想用：status" in out
    assert "invalid choice" not in out
    assert "agenthub --help" in out


def test_unknown_command_no_false_suggestion(capsys):
    """差太远的词不要硬凑建议（宁缺勿误导）。"""
    assert cli.main(["zzzz"]) == 2
    out = capsys.readouterr().out
    assert "未知命令：zzzz" in out
    assert "你可能想用" not in out


def test_known_command_still_dispatches(capsys):
    """预扫不能把正常命令拦下去（call 的第一个位置参数是工具名，不是子命令）。"""
    assert cli.main(["tools"]) == 0
    assert "hub_search" in capsys.readouterr().out
