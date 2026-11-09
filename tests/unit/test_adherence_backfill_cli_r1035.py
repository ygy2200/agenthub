# -*- coding: utf-8 -*-
"""R1035：把离线补扫做成一条命令（`cli.py adherence-backfill`）。

为什么值得一个用例：R876 的工具一直在 tools/ 下，但巡检红了两周没人（包括我自己）
去敲它——「有工具」不等于「有出口」。这条钉住三件事：
  ① 子命令真的注册在 CLI 里（--help 能看到）；
  ② 预演不写账（默认安全）；
  ③ 巡检的 fix 文案点名这条命令（否则读的人还是不知道跑哪个）。
"""
import io
import contextlib
import pathlib

import brain
import cli


def test_command_registered():
    p = pathlib.Path(__file__).resolve().parents[2] / "cli.py"
    src = p.read_text(encoding="utf-8")
    assert '"adherence-backfill"' in src
    assert "def cmd_adherence_backfill" in src


def test_dry_run_on_empty_root_returns_zero(tmp_path):
    root = str(tmp_path / "hub")
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.cmd_adherence_backfill(root, apply=False, days=30, min_idle_h=2.0)
    assert rc == 0, buf.getvalue()[:300]
    assert "预演" in buf.getvalue()


def test_check_fix_points_to_command():
    import ops
    txt = ops.CHECK_FIXES["adherence"] if hasattr(ops, "CHECK_FIXES") else ""
    assert "adherence-backfill" in txt, "巡检 fix 文案没点名新命令"
