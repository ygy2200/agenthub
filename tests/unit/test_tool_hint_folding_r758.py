# -*- coding: utf-8 -*-
r"""R758：提示行的「等 N 个」没有出口（B009）。

R746 为了让提示行能照抄，把骨架补全了；代价是超长，于是未知工具折叠成
「xxx 等 N 个」。问题是折叠之后**没有任何办法看到剩下的名字**——用户照着提示
做不下去，只能猜（这正是 R739 砍参数时犯过的同一类错：为行长牺牲可用性）。

口径：任何折叠都必须带一个能拿到全量的入口，且入口是真实存在的命令
（`cli.py tools` 会逐行列出全部 MCP 工具名与说明）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402


UNKNOWN = ["hub_alpha", "hub_beta", "hub_gamma", "hub_delta", "hub_epsilon"]


def test_folded_unknown_tools_point_at_tools_command():
    line = cli._tool_hint_line(UNKNOWN, where="修复行")
    assert "等 5 个" in line, "折叠本身要保留（行长仍是约束）"
    assert "cli.py tools" in line, "折叠后必须给出全量入口"


def test_folded_known_tools_also_have_outlet():
    """已知工具超 3 个时同样被 `[:3]` 截断——这条截断此前连「等 N 个」都没有。"""
    known = ["hub_claim", "hub_next", "hub_tasks", "hub_task_update", "hub_digest"]
    line = cli._tool_hint_line(known, where="修复行")
    assert "cli.py tools" in line
    assert "hub_task_update→" not in line or "等" in line


def test_short_hint_stays_quiet():
    """≤3 个且都已知时不加噪音——这句话在 doctor 输出里每跑一次都出现。"""
    line = cli._tool_hint_line(["hub_claim", "hub_next"], where="修复行")
    assert "cli.py tools" not in line
    assert "等" not in line


def test_tools_command_exists_for_real():
    """入口不能是画饼：`cli.py tools` 必须在 argparse 里注册且能列出 hub_ 工具名。"""
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["tools"])
    assert rc == 0
    out = buf.getvalue()
    assert "hub_status" in out and "hub_claim" in out
