# -*- coding: utf-8 -*-
"""CLI --help 面向使用者：不出现 R79/R42/X5 这类内部轮次号，且每个子命令自带说明（R177）。"""
import re

import pytest


def _help(capsys, argv):
    import cli
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_top_help_has_examples_and_no_internal_ids(capsys):
    out = _help(capsys, ["--help"])
    assert "常用示例" in out and "agenthub next --agent" in out
    assert "根目录：--root" in out
    # R708 起多了 -V/--version（排障第一问的出口），usage 行随之带上 [-V]。
    assert re.search(r"usage: agenthub \[-h\] \[--root ROOT\] \[-V\] 命令", out)
    assert "--version" in out and "打印版本号" in out
    assert not re.search(r"\b[RX]\d+\b", out), "顶层帮助仍含内部编号"


def test_next_help_lists_six_groups(capsys):
    out = _help(capsys, ["next", "--help"])
    for word in ("可认领", "进行中", "可接管", "等待依赖", "退避", "延后"):
        assert word in out, f"next --help 缺少「{word}」"
    assert "--agent" in out and "每组最多几条" in out


def test_every_subcommand_help_is_human_readable(capsys):
    top = _help(capsys, ["--help"])
    names = re.findall(r"^    ([a-z][a-z-]+)\s{2,}", top, re.M)
    assert len(names) >= 25, names
    bad = []
    for name in names:
        out = _help(capsys, [name, "--help"])
        if re.search(r"\b[RX]\d+\b", out):
            bad.append(name)
        head = out.split("\n\n")
        assert not (len(head) > 1 and not head[1].strip()), f"{name} --help 缺少说明"
    assert not bad, f"这些子命令帮助仍含内部编号：{bad}"
