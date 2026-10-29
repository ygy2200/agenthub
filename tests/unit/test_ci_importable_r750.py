# -*- coding: utf-8 -*-
r"""R750：单元层 import 的本地模块必须被 git 跟踪，否则 CI 在收集期就红。

实测（2026-10-29，提交 1e53730 的 CI 日志）：
    ERROR collecting tests/unit/test_publish_upload_progress_r742.py
    E   ModuleNotFoundError: No module named 'publish'
根因不是代码错，而是 `publish.py` 被写进了 .gitignore——本机有、CI 检出后没有。
这类「本机能跑、CI 必红」的偏差靠人记不住，做成门禁：扫 tests/unit 的**直接 import**，
凡是仓库里真实存在（`x.py` 或目录）的模块，都必须 `git ls-files` 查得到。
"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "unit"


def _git(args):
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def _imported_local_modules(tests_dir=TESTS):
    names = set()
    for f in sorted(Path(tests_dir).glob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:            # 语法坏的用例由 pytest 自己报，这里不重复
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                names.add(n.module.split(".")[0])
    return {n for n in names if (ROOT / f"{n}.py").exists() or (ROOT / n).is_dir()}


def _untracked(names):
    """返回 names 里没有被 git 跟踪的（模块名 → 顶层文件）。"""
    out = []
    for n in sorted(names):
        p = ROOT / f"{n}.py"
        if p.exists():
            args = ["ls-files", "--error-unmatch", f"{n}.py"]
        else:
            args = ["ls-files", "--", n]
        r = _git(args)
        if r.returncode != 0 or not r.stdout.strip():
            out.append(n)
    return out


@pytest.mark.skipif(_git(["rev-parse", "--git-dir"]).returncode != 0,
                    reason="不在 git 工作树里（比如打包产物），门禁无从判断")
def test_every_imported_local_module_is_tracked():
    missing = _untracked(_imported_local_modules())
    assert not missing, (
        f"这些模块被 tests/unit 直接 import 但没入库，CI 会在收集期时报 "
        f"ModuleNotFoundError：{missing}。要么 git add，要么把用例改成 importorskip。")


def test_gate_bites_on_a_name_that_cannot_be_tracked():
    """反证：门禁必须能报红，否则它只是装饰。"""
    assert _untracked({"definitely_not_tracked_r750"}) == ["definitely_not_tracked_r750"]
