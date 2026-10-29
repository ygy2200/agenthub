# -*- coding: utf-8 -*-
"""R732：空态与纠错要给「能粘的命令」，不是工具名。

实测（空大脑 /tmp/hubtest1）：
  · `tasks` 空态 → 「暂无任务（hub_task_add 新建…）」。纯 CLI 用户照敲 `hub_task_add`
    是 command not found（那是 MCP 工具名，不是命令）；这一屏恰好是「第一次打开任务看板」
    唯一能看到的东西，等于死路。
  · `next` 空态 → 「前沿队列为空（没有待办/在办任务）。」没有下一句。
  · `todo-list` → 「你可能想用：tasks」还要人再猜一次怎么敲。

验收口径：提示里给出的命令必须**真的跑得通**——本用例把提示里的命令抓出来执行一遍。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402


def _fresh(tmp_path):
    assert cli.main(["init", "--root", str(tmp_path)]) == 0
    return str(tmp_path)


def test_tasks_empty_state_is_runnable(capsys, tmp_path):
    root = _fresh(tmp_path)
    assert cli.main(["tasks", "--root", root]) == 0
    out = capsys.readouterr().out
    assert "暂无任务" in out
    assert "hub_task_add" in out and f"{cli.CMD} call hub_task_add" in out
    assert f"{cli.CMD} help tasks" in out

    # 抓出提示里的工具名与参数名，按提示真正建一条任务
    m = re.search(rf"{re.escape(cli.CMD)} call (\w+)", out)
    assert m and m.group(1) == "hub_task_add"
    assert cli.main(["call", "hub_task_add", "title=按提示建的任务",
                     "project=AgentHub-多agent文件统一管理", "agent=zcode",
                     "--root", root]) == 0
    assert cli.main(["tasks", "--root", root]) == 0
    after = capsys.readouterr().out
    assert "按提示建的任务" in after  # 提示 → 执行 → 立刻可见


def test_next_empty_state_is_runnable(capsys, tmp_path):
    root = _fresh(tmp_path)
    assert cli.main(["next", "--agent", "zcode", "--root", root]) == 0
    out = capsys.readouterr().out
    assert "前沿队列为空" in out
    assert f"{cli.CMD} call hub_task_add" in out
    assert f"{cli.CMD} tasks" in out


def test_unknown_command_hint_is_runnable(capsys):
    assert cli.main(["todo-list"]) == 2
    out = capsys.readouterr().out
    m = re.search(rf"用法与示例：{re.escape(cli.CMD)} help (\w+)", out)
    assert m, out
    capsys.readouterr()
    # 提示里的那条命令真的能跑（不是又一句空话）
    assert cli.main(["help", m.group(1)]) == 0
    assert "任务看板" in capsys.readouterr().out
