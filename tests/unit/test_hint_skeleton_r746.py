# -*- coding: utf-8 -*-
r"""R746：R739/R745 的换算行把「参数骨架」砍成了裸子命令。

实测（真实库 `cli.py digest`，v2.41.160）：
    提示：…命令行改用——hub_claim→python cli.py claim / hub_task_update→python cli.py update。
照抄第一条：
    usage: agenthub claim [-h] [--root ROOT] --agent AGENT task_id
    agenthub claim: error: the following arguments are required: task_id, --agent
——R739 为压行长用了 `native[t].split(' ')[0]`，把必填参数一并砍掉，
于是「提示即命令」在这一层仍然是「提示即猜」。同一族的第三处（R735 正文、R739 修复行、R745 建议行）。

另一处语义错位：`hub_handoff` 被映射到 `handoffs`（**只读**清单命令），
而 hub_handoff 是写入工具（create/claim/close）——照着敲只会看到一张空的清单。

口径：①有原生命令的，骨架必须带必填占位符，且把占位符换成真值后**真能解析**；
②没有原生命令的写入工具不许冒充只读命令。
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402


def _issue(fix):
    return {"name": "tasks_health", "level": "warn", "msg": "x", "fix": fix}


def test_hint_keeps_required_placeholders():
    line = cli._mcp_tool_hint([_issue("hub_claim 认领；hub_task_update 关单")])
    assert f"{cli.CMD} claim <id> --agent" in line
    assert f"{cli.CMD} update <id>" in line
    # 裸子命令（缺 <id>/--agent）不许再出现
    assert f"{cli.CMD} claim /" not in line and not line.rstrip("。").endswith(f"{cli.CMD} claim")


def _fragments(line):
    """从提示行里抠出 `python cli.py …` 片段（到 / 或 ；或 。为止）。"""
    out = []
    for m in re.finditer(re.escape(cli.CMD) + r"\s+([^/；。]+)", line):
        out.append(m.group(1).strip())
    return out


_FILL = {"<id>": "1", "<状态>": "done"}


def test_every_hint_parses_with_placeholders_filled(tmp_path):
    """占位符填上假值后交给真解析器。

    不能用 `--help` 验：argparse 见到 --help 会绕过必填参数检查，坏骨架也能退 0。
    改成指向一个「不是大脑的临时目录」——参数被接受才会走到根目录闸门（返回 2）；
    argparse 拒绝必填参数时抛 SystemExit，两者可区分。
    """
    assert cli._TOOL_CLI_FORM, "映射表空了，用例失去意义"
    for tool in cli._TOOL_CLI_FORM:
        line = cli._mcp_tool_hint([_issue(f"{tool} 一下")])
        frags = _fragments(line)
        assert frags, f"{tool} 的提示行里没有可执行片段：{line}"
        for frag in frags:
            filled = re.sub(r"<[^>]+>", lambda m: _FILL.get(m.group(0), "probe"), frag)
            tokens = filled.split() + ["--root", str(tmp_path)]
            try:
                rc = cli.main(tokens)
            except SystemExit as e:  # argparse 拒绝必填参数
                pytest.fail(f"{tool} 的骨架照抄解析失败（退 {e.code}）：{frag}")
            assert rc == 2, f"{tool} 的骨架没走到根目录闸门（返回 {rc}）：{frag}"


def test_handoff_is_not_mapped_to_read_only_list():
    line = cli._mcp_tool_hint([_issue("hub_handoff 留一份交接")])
    assert f"{cli.CMD} handoffs" not in line, "hub_handoff 是写操作，不能指向只读清单"
    assert "call hub_handoff" in line
