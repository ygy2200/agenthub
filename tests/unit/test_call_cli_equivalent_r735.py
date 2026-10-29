# -*- coding: utf-8 -*-
"""R735：`call` 打出来的后续指引，得能在 CLI 里接着用。

实测（空脑）：
    $ python cli.py call hub_task_add title=试一下 project=… agent=zcode
    任务已建 #1：试一下
    认领：hub_claim；下一步推荐：hub_next；列表：hub_tasks
那三个名字是 MCP 工具名——在 CLI 里照抄是 command not found。正文不能改
（MCP 面读它是对的），所以 CLI 侧补一行等价写法，且只列正文里真出现过的。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402


def _fresh(tmp_path) -> str:
    assert cli.main(["init", "--root", str(tmp_path)]) == 0
    return str(tmp_path)


def test_cli_equivalent_maps_only_present_tools():
    assert cli._cli_equivalent("认领：hub_claim；下一步推荐：hub_next；列表：hub_tasks") == \
        "claim <id> --agent <名字> · next --agent <名字> · tasks"
    assert cli._cli_equivalent("这条正文没提任何工具") == ""


def test_task_add_prints_cli_equivalent(capsys, tmp_path):
    root = _fresh(tmp_path)
    capsys.readouterr()
    assert cli.main(["call", "hub_task_add", "title=试一下",
                     "project=AgentHub-多agent文件统一管理", "agent=zcode",
                     "--root", root]) == 0
    out = capsys.readouterr().out
    assert "hub_claim" in out                       # MCP 正文原样保留
    assert "（CLI 写法：" in out
    assert "next --agent <名字>" in out and "tasks" in out


def test_error_result_has_no_noise_footer(capsys, tmp_path):
    root = _fresh(tmp_path)
    capsys.readouterr()
    assert cli.main(["call", "hub_search", "--root", root]) != 0  # 缺必填参数
    out = capsys.readouterr().out
    assert "CLI 写法" not in out


def _subcommands(capsys):
    assert cli.main(["help"]) == 0
    out = capsys.readouterr().out
    names, started = set(), False
    for line in out.splitlines():
        if line.startswith("positional arguments"):
            started = True
            continue
        if started:
            if not line.strip():
                break
            m = re.match(r"\s{2,}(\S+)", line)
            if m:
                names.add(m.group(1))
    return names


def test_every_equivalent_names_a_real_cli_command(capsys):
    names = _subcommands(capsys)
    assert "tasks" in names and "claim" in names  # 解析本身没跑偏
    for form in cli._TOOL_CLI_FORM.values():
        head = re.split(r"\s+", form.strip())[0]
        if head == "call":
            continue  # `call hub_*` 由 call 自己承载，不查子命令表
        assert head in names, f"CLI 写法指向不存在的命令：{form}"
