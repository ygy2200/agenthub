# -*- coding: utf-8 -*-
"""R753：新用户的第一屏永远看不到——裸跑命令在「建脑」之前就被根目录闸门拦下。

实测（CI 全新环境，没有 ~/.agenthub/config.json）：
    test_bare_invocation_shows_first_steps - assert 2 == 0
即 `agenthub` 直接退 2、只打印「未找到大脑根目录」；本机因为有配置，一直是绿的。
R712 专门做的第一屏（三步上手 + 常用命令）对**最需要它的人**不可达。
口径：裸跑是「问怎么用」，不是「执行命令」——没有大脑也应给上手引导，退出码 0。
有大脑时仍要显示当前根目录。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402
import core  # noqa: E402


@pytest.fixture
def no_config(monkeypatch):
    monkeypatch.setattr(core, "load_config", lambda: {})
    return monkeypatch


def test_bare_run_without_brain_still_shows_first_steps(no_config, capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "第一次用" in out
    assert "init --root" in out            # 没有大脑时第一步就该是建脑
    assert "未找到大脑根目录" not in out, "闸门话术不该顶掉上手引导"
    assert "（未配置）" in out


def test_bare_run_with_brain_shows_root(no_config, capsys, tmp_path):
    root = tmp_path / "brain"
    root.mkdir()
    no_config.setattr(core, "load_config", lambda: {"root": str(root)})
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "第一次用" in out and str(root) in out


def test_other_commands_still_guard_on_root(no_config, capsys):
    """闸门只为「裸跑」让路；真执行命令时缺大脑仍要退 2 并给建脑路径。"""
    assert cli.main(["status"]) == 2
    assert "未找到大脑根目录" in capsys.readouterr().out
