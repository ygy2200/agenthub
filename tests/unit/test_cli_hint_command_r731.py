# -*- coding: utf-8 -*-
"""R731：提示句里的命令必须能照着敲。

实测（本机）：`python cli.py --help` 的「第一次用（三步）」教人敲
`agenthub init --root "<目录>"`——而本机没有 `agenthub` 这个可执行文件，
README 全篇写的是 `python cli.py`；照抄第一条就 command not found。
同一份 CLI 里当时并存三种前缀（`agenthub ` 21 处、`python cli.py ` 36 处、
裸 `cli.py ` 5 处），用户不知道该信哪句。

本条把「提示 = 可粘贴」变成门禁，三条不变量：
  ① 用户可见输出里不许出现以 `agenthub` 开头的可粘贴命令行（那是产品名不是命令）；
  ② `python cli.py <子命令>` 里的子命令必须真实存在（防手滑写出不存在的命令）；
  ③ `CMD` 是单一真源，改前缀只改一处。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402

# 「看起来像可粘贴命令」的行：行首（允许缩进）就是命令本身。
_HINT_CMD = re.compile(r"^\s*python cli\.py\s+([-\w]+)")
_BARE_PRODUCT = re.compile(r"^\s*agenthub\s+[-\w]")
_KNOWN_PLACEHOLDERS = {"--help", "-h", "-V", "--version"}


def _subcommands_from_help(capsys):
    """从 `help`（= parser 全量帮助）的 positional 段抓真实子命令名。"""
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
    return names, out


def _check_hints(text, names, where):
    bad_prefix = [ln.strip() for ln in text.splitlines() if _BARE_PRODUCT.match(ln)]
    assert not bad_prefix, f"{where} 出现了不能照抄的 agenthub 前缀：{bad_prefix}"
    unknown = []
    for line in text.splitlines():
        m = _HINT_CMD.match(line)
        if not m:
            continue
        sub = m.group(1)
        if sub in _KNOWN_PLACEHOLDERS:
            continue
        if sub not in names:
            unknown.append((sub, line.strip()))
    assert not unknown, f"{where} 指向了不存在的子命令：{unknown}"


def test_cmd_prefix_is_single_source():
    assert cli.CMD == "python cli.py"


def test_help_screen_hints_are_pasteable(capsys):
    names, out = _subcommands_from_help(capsys)
    assert "tasks" in names and "doctor" in names  # 抓取本身没跑偏
    _check_hints(out, names, "--help")


def test_bare_screen_hints_are_pasteable(capsys):
    names, _ = _subcommands_from_help(capsys)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    _check_hints(out, names, "裸跑首屏")
    assert "python cli.py init" in out and "python cli.py onboard" in out


def test_unknown_command_message_is_pasteable(capsys):
    names, _ = _subcommands_from_help(capsys)
    assert cli.main(["todo-list"]) == 2
    out = capsys.readouterr().out
    _check_hints(out, names, "未知命令纠错")
    assert "python cli.py --help" in out


def test_root_hint_is_pasteable(capsys, monkeypatch):
    names, _ = _subcommands_from_help(capsys)
    monkeypatch.setattr(cli.core, "get_root", lambda: "")
    assert cli.main(["root"]) == 1
    out = capsys.readouterr().out
    _check_hints(out, names, "root 空配置提示")
    assert "python cli.py init" in out
