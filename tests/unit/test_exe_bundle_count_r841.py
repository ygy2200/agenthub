# -*- coding: utf-8 -*-
"""R841：exe 随包体检的通过语不能写死条数（B039 收口）。

为什么改：`tools/exe_bundle_check.py` 的通过语写死「16 个」，而随包文件已涨到 20 个，
输出变成「随包文件：20/20」紧跟「✓ 16 个运行时文件……」——结论对、文案错。这种自相
矛盾的体检最容易被当成「工具坏了」直接无视，而它恰恰是唯一能证明「打包链没断」的量具。

修法：抽 `ok_line(n)`，条数从 `core.bundle_files()` 现算；同时把 B033 的验收口径写清楚——
`AgentHub.exe deploy` 这条验法不成立（`main.py` 只调 `run_gui()`，不看 argv），
真正要证的是「归档里有没有这 20 个文件」，所以是静态读 CArchive，不是跑它。

本文件钉两件事：
  1. 通过语里的数字随 `core.bundle_files()` 变化（不是常量）；
  2. 传 3 就写 3——防止有人又把它写成固定串。

「修之前会怎样失败」：第 1 条在旧代码上必红——旧实现根本没有 `ok_line` 这个函数。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import core  # noqa: E402
import exe_bundle_check as checker  # noqa: E402


def test_ok_line_uses_real_count():
    n = len(core.bundle_files())
    line = checker.ok_line(n)
    assert f"✓ {n} 个运行时文件" in line, line
    assert "16 个" not in line or n == 16, "数字又写死了？"


def test_ok_line_follows_argument():
    assert checker.ok_line(3).startswith("✓ 3 个运行时文件")
    assert checker.ok_line(41).startswith("✓ 41 个运行时文件")
