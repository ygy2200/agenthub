# -*- coding: utf-8 -*-
"""R226：`--root` 位置无关——`agenthub doctor --root X` 与 `agenthub --root X doctor` 等价。

R225 实跑 doctor 时撞到 `unrecognized arguments: --root`：argparse 的全局选项只在
子命令**之前**生效，而人的直觉是把参数写在动作后面。修法是给每个子命令补一个
`--root`（`default=argparse.SUPPRESS`，没写时不覆盖全局值）。
"""
import pytest

import cli


def _spy_doctor(monkeypatch):
    seen = []

    def fake_doctor(root, full=False, **flags):
        """R237 起 doctor 还有 --json/--only/--skip/--no-machine/--slow；本用例只关 --root 位置。"""
        seen.append((root, full))
        return 0

    monkeypatch.setattr(cli, "cmd_doctor", fake_doctor)
    return seen


def test_root_before_subcommand(monkeypatch, tmp_path):
    seen = _spy_doctor(monkeypatch)
    assert cli.main(["--root", str(tmp_path), "doctor"]) == 0
    assert seen == [(str(tmp_path), False)]


def test_root_after_subcommand(monkeypatch, tmp_path):
    seen = _spy_doctor(monkeypatch)
    assert cli.main(["doctor", "--root", str(tmp_path)]) == 0
    assert seen == [(str(tmp_path), False)]


def test_root_equals_form_both_positions(monkeypatch, tmp_path):
    seen = _spy_doctor(monkeypatch)
    assert cli.main([f"--root={tmp_path}", "doctor", "--full"]) == 0
    assert cli.main(["doctor", "--full", f"--root={tmp_path}"]) == 0
    assert seen == [(str(tmp_path), True), (str(tmp_path), True)]


def test_subcommand_without_root_keeps_global_value(monkeypatch, tmp_path):
    """子命令不写 --root 时必须沿用全局值——SUPPRESS 的回归守卫。"""
    seen = _spy_doctor(monkeypatch)
    assert cli.main(["--root", str(tmp_path), "doctor", "--full"]) == 0
    assert seen == [(str(tmp_path), True)]


def test_unknown_option_still_errors(monkeypatch, tmp_path):
    """补参不能把拼错的选项也放行。"""
    seen = _spy_doctor(monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["doctor", "--bogus"])
    assert seen == []


def test_call_root_between_tool_and_kv(monkeypatch, tmp_path):
    """R295：`call <tool> --root X key=value` 曾被 argparse 拒绝（nargs='*' 与穿插选项冲突）。"""
    seen = []

    def fake_call(root, tool, kv):
        seen.append((root, tool, kv))
        return 0

    monkeypatch.setattr(cli, "cmd_call", fake_call)
    assert cli.main(["call", "hub_search", "--root", str(tmp_path), "keyword=x", "limit=5"]) == 0
    assert seen == [(str(tmp_path), "hub_search", ["keyword=x", "limit=5"])], seen


def test_call_root_equals_form_after_kv(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(cli, "cmd_call",
                        lambda root, tool, kv: seen.append((root, tool, kv)) or 0)
    assert cli.main(["call", "hub_search", "keyword=x", f"--root={tmp_path}"]) == 0
    assert seen == [(str(tmp_path), "hub_search", ["keyword=x"])], seen


def test_hoist_root_keeps_missing_value():
    """`--root` 缺值时不吞别的参数，交给 argparse 报用法错误。"""
    assert cli._hoist_root(["call", "hub_search", "--root"]) == ["call", "hub_search", "--root"]
    assert cli._hoist_root(["doctor", "--root", "D:/x", "--full"]) == \
        ["--root", "D:/x", "doctor", "--full"]
