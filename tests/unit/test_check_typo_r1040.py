# -*- coding: utf-8 -*-
"""R1040：`cli.py check <拼错的名字>` 要给最近匹配，而不是只回「未注册」。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(arg):
    return subprocess.run([sys.executable, str(ROOT / "cli.py"), "check", arg],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


def test_typo_suggests_nearest():
    p = _run("adhrence")
    assert p.returncode == 2
    assert "你是不是想跑：adherence" in (p.stderr or ""), p.stderr[:300]


def test_short_prefix_suggests_family():
    p = _run("backup")
    assert "backup_health" in (p.stderr or ""), p.stderr[:300]
