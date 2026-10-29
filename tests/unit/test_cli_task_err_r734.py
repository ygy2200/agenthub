# -*- coding: utf-8 -*-
"""R734：任务流转失败别把前缀说两遍，也别把人留在原地。

实测（真库 tmp）：
    $ python cli.py claim 1 --agent hermes
    认领失败：认领失败：任务 #1 已被 zcode 认领（…）
`brain.task_claim` 返回的 err 自带「认领失败：」前缀（它同时服务 MCP 面），
CLI 又拼了一遍。同族还有 延后/重试/唤醒/审批/退回——第二个人来抢同一条任务时
**必然**看到这句，是这套 CLI 最容易被截图嘲笑的地方。

同时：失败只报原因、不给下一步。对照 `call` 的失败（R9 起就有「下一步：」），
同一份 CLI 两套口径。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402


def _fresh(tmp_path) -> str:
    assert cli.main(["init", "--root", str(tmp_path)]) == 0
    return str(tmp_path)


def _add_task(root: str, title="抢手任务") -> None:
    assert cli.main(["call", "hub_task_add", "title=" + title,
                     "project=AgentHub-多agent文件统一管理", "agent=zcode",
                     "--root", root]) == 0


def test_err_line_dedupes_prefix():
    assert cli._err_line("认领", "认领失败：任务 #1 已被 zcode 认领").startswith(
        "认领失败：任务 #1 已被 zcode 认领")
    # 只出现一次
    line = cli._err_line("认领", "认领失败：任务 #1 已被 zcode 认领")
    assert line.count("认领失败") == 1


def test_err_line_adds_prefix_when_missing():
    line = cli._err_line("更新", "任务 #1 由 zcode 持有")
    assert line.startswith("更新失败：任务 #1 由 zcode 持有")


def test_err_line_always_has_next_step():
    for action, err in [("认领", "任务 #999 不存在"),
                        ("认领", "任务 #1 已被 zcode 认领（…）"),
                        ("认领", "任务 #1 前置未完成——等待 #2"),
                        ("更新", "任务 #1 由 zcode 持有"),
                        ("重试", "死信"),
                        ("认领", "")]:
        assert "下一步：" in cli._err_line(action, err), (action, err)


def test_claim_conflict_says_it_once(capsys, tmp_path):
    root = _fresh(tmp_path)
    capsys.readouterr()
    _add_task(root)
    capsys.readouterr()
    assert cli.main(["claim", "1", "--agent", "zcode", "--root", root]) == 0
    capsys.readouterr()
    assert cli.main(["claim", "1", "--agent", "hermes", "--root", root]) == 1
    out = capsys.readouterr().out
    assert out.count("认领失败") == 1, out
    assert "下一步：" in out


def test_claim_missing_task_points_at_tasks(capsys, tmp_path):
    root = _fresh(tmp_path)
    capsys.readouterr()
    assert cli.main(["claim", "999", "--agent", "hermes", "--root", root]) == 1
    out = capsys.readouterr().out
    assert "不存在" in out and f"{cli.CMD} tasks" in out


def test_next_step_never_names_a_missing_flag():
    """提示里出现的 --flag 必须是 tasks/claim/update 真有的，别发明参数。"""
    import argparse
    p = argparse.ArgumentParser()
    hints = [cli._err_next_step(x) for x in
             ("不存在", "已被 zcode 认领", "前置未完成", "由 zcode 持有", "WIP 上限", "冻结", "reason")]
    known = {"--agent", "--owner", "--status", "--reason", "--defer-reason", "--off"}
    seen = set()
    for h in hints:
        for tok in h.split():
            if tok.startswith("--"):
                seen.add(tok)
    assert seen <= known, f"提示里出现了未核实的参数：{seen - known}"
