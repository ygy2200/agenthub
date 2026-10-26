# -*- coding: utf-8 -*-
"""工具脚本控制台守卫（R179）：GBK 终端/管道下打印 ✓ 不崩，输出统一 UTF-8。"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
DOCUMENTED = (
    "gui_snapshot", "real_walkthrough", "search_walkthrough", "signal_walkthrough",
    "project_walkthrough", "hub_walkthrough", "playbook_walkthrough",
    "governance_walkthrough", "help_walkthrough", "palette_walkthrough",
    "connect_walkthrough", "today_walkthrough", "perf_walkthrough",
    "audit_errmsg", "stale_eval", "soak_test", "ui_audit",
)


def test_console_guard_survives_gbk_stdout():
    env = dict(os.environ, PYTHONIOENCODING="gbk")
    r = subprocess.run(
        [sys.executable, "-c", "import _console; print('✓ 中文')"],
        cwd=TOOLS, capture_output=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert "✓ 中文" in r.stdout.decode("utf-8")


def test_documented_tools_import_console_guard():
    missing = [name for name in DOCUMENTED
               if "import _console" not in (TOOLS / f"{name}.py").read_text(encoding="utf-8")]
    assert not missing, f"这些走查工具未接入 UTF-8 守卫：{missing}"
