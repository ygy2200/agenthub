# -*- coding: utf-8 -*-
"""gui 层：空库空态横扫门禁（R756，B005 的第一步）。

手工逐页截图不可复跑，B005 因此连续四批顺延。本用例把 tools/gui_empty_sweep.py
跑成门禁：空库下 18 页每页都要么有标准空态、要么本来就有内容（内置流程模板），
不允许出现「列表空着却没有任何引导」。

必须子进程：offscreen QApplication 与全局状态在 pytest 进程里会互相污染
（与 test_snapshot_baseline 同一理由）。
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_empty_state_sweep_is_clean():
    import pytest
    pytest.importorskip("PySide6")

    with tempfile.TemporaryDirectory(prefix="empty_sweep_") as tmp:
        root = Path(tmp) / "hub"
        out = Path(tmp) / "shots"
        p = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "gui_empty_sweep.py"),
             "--root", str(root), "--out", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT), timeout=300)
    assert p.returncode == 0, f"空态走查有缺陷：\n{p.stdout}\n{p.stderr}"
    assert "全部页面通过空态检查" in p.stdout
    assert "18 页" in p.stdout, "页面数骤降说明有页面没被走查到"
