# -*- coding: utf-8 -*-
"""R711/R712/R713：CLI 首屏与机器契约。

R711：`--json` 是给脚本用的契约，出错也必须吐 JSON（原来路径打错时吐人话 + 退 2，
      脚本拿到的是半截文本，报错变成 JSONDecodeError，真实原因被吃掉）。
R712：裸跑 `agenthub` 只打印 30+ 条 argparse 清单，新用户看不到「先做什么」。
R713：`agenthub help <命令>` 此前是未知命令，用户得记住 `<命令> --help` 这种反直觉写法。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402


def test_json_mode_emits_json_on_bad_root(capsys, tmp_path):
    missing = tmp_path / "not-a-brain"
    assert cli.main(["status", "--json", "--root", str(missing)]) == 2
    out = capsys.readouterr().out.strip()
    data = json.loads(out)  # 解析不了就说明契约破了
    assert data["ok"] is False
    assert str(missing).replace("\\", "/") in data["error"].replace("\\", "/")
    assert "init" in data["error"]  # 错误里必须带走出来的路


def test_human_mode_unchanged(capsys, tmp_path):
    assert cli.main(["status", "--root", str(tmp_path / "not-a-brain")]) == 2
    out = capsys.readouterr().out
    assert "大脑根目录不存在" in out
    assert not out.lstrip().startswith("{")


def test_bare_invocation_shows_first_steps(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "第一次用" in out and "agenthub status" in out
    assert "agenthub help <命令>" in out
    assert "positional arguments" not in out  # 不是 argparse 原样清单


def test_help_subcommand(capsys):
    assert cli.main(["help", "tasks"]) == 0
    out = capsys.readouterr().out
    assert "usage: agenthub tasks" in out and "任务看板" in out


def test_help_unknown_topic(capsys):
    assert cli.main(["help", "nope"]) == 2
    assert "未知命令：nope" in capsys.readouterr().out
