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
