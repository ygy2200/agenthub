# -*- coding: utf-8 -*-
"""R736：交接与租约的空态同样是死路。

实测（空脑）：
    $ python cli.py handoffs
    （无未认领交接）            ← 没有下一句
    $ python cli.py locks
    暂无文件租约（hub_lock action=acquire 或 cli.py lock <path> --agent ...）
                                     ↑裸 `cli.py`（既不是 python cli.py，也不是产品名）

`handoffs` 的空态尤其要紧：它是「接手别人留下的活」唯一的入口，只报「无」等于把人挡在门外。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402

_BARE_CLI = re.compile(r"(?<!python )cli\.py")  # `python cli.py` 合法，裸 `cli.py` 不合法


def _fresh(tmp_path) -> str:
    assert cli.main(["init", "--root", str(tmp_path)]) == 0
    return str(tmp_path)


def test_handoffs_empty_state_is_runnable(capsys, tmp_path):
    root = _fresh(tmp_path)
    capsys.readouterr()
    assert cli.main(["handoffs", "--root", root]) == 0
    out = capsys.readouterr().out
    assert "（无未认领交接）" in out
    assert f"{cli.CMD} call hub_handoff action=create" in out
    assert f"{cli.CMD} help handoffs" in out
    assert not _BARE_CLI.search(out), out

    # 把提示里的命令抓出来真执行一次，确认不是空话
    assert cli.main(["call", "hub_handoff", "action=create",
                     "project=AgentHub-多agent文件统一管理", "agent=zcode",
                     "done=做完了什么", "next_step=下一步做什么", "--root", root]) == 0
    capsys.readouterr()
    assert cli.main(["handoffs", "--root", root]) == 0
    assert "交接 1 条" in capsys.readouterr().out


def test_locks_empty_state_uses_full_prefix(capsys, tmp_path):
    root = _fresh(tmp_path)
    capsys.readouterr()
    assert cli.main(["locks", "--root", root]) == 0
    out = capsys.readouterr().out
    assert "暂无文件租约" in out
    assert f"{cli.CMD} lock" in out
    assert f"{cli.CMD} call hub_lock action=acquire" in out
    assert not _BARE_CLI.search(out), out


def test_tasks_approvals_empty_is_informative(capsys, tmp_path):
    root = _fresh(tmp_path)
    capsys.readouterr()
    assert cli.main(["tasks", "--approvals", "--root", root]) == 0
    out = capsys.readouterr().out
    assert "审批队列为空" in out
    assert not _BARE_CLI.search(out), out
