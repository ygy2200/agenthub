# -*- coding: utf-8 -*-
"""R1119：node 可执行文件不许硬编码开发机路径。

R1118 补了 `actions/setup-node` 后 CI 仍 26 failed，日志实锤：
`args = 'D:/node/node.exe ... brain_error_recall.mjs'` → `FileNotFoundError [WinError 2]`。
setup-node 装到 PATH 里的 node 根本没被用，因为三个端到端钩子测试把
`NODE = r"D:/node/node.exe"` 写死了（本机有 D 盘，CI runner 没有）。

修法不是「CI 再造一个 D 盘」，是让解析器按
`AGENTHUB_NODE 环境变量 → shutil.which("node") → 本机兜底路径` 找，
找不到就 skip 并写明原因（pytest 官方 skipping 口径：依赖外部资源要显式标记）。

这四条钉：共享解析器存在 / 三个文件不再有裸硬编码 / 优先级正确 / 全找不到时返回空。
"""
import pathlib
import re
import sys

import pytest

UNIT = pathlib.Path(__file__).resolve().parent
HOOK_TESTS = ("test_hook_node_error_r986.py",
              "test_hook_traceback_r971.py",
              "test_hook_transcript_r1029.py")


def _mod():
    if str(UNIT) not in sys.path:
        sys.path.insert(0, str(UNIT))
    import _node_exe
    return _node_exe


def test_shared_resolver_exists():
    assert callable(_mod().node_exe)


def test_no_bare_hardcoded_node_constant():
    for name in HOOK_TESTS:
        src = (UNIT / name).read_text(encoding="utf-8")
        assert not re.search(r'^NODE\s*=\s*r?["\']D:/node/node\.exe', src, re.M), (
            f"{name} 仍把 NODE 写死在开发机盘符")


def test_resolver_priority_env_then_path(monkeypatch, tmp_path):
    m = _mod()
    fake = tmp_path / "node.exe"
    fake.write_bytes(b"x")
    monkeypatch.setenv("AGENTHUB_NODE", str(fake))
    assert m.node_exe() == str(fake), "环境变量优先级最高"
    monkeypatch.delenv("AGENTHUB_NODE")
    monkeypatch.setattr(m.shutil, "which", lambda _n: str(fake))
    assert m.node_exe() == str(fake), "PATH 命中该排第二"


def test_resolver_returns_empty_when_nowhere(monkeypatch):
    m = _mod()
    monkeypatch.delenv("AGENTHUB_NODE", raising=False)
    monkeypatch.setattr(m.shutil, "which", lambda _n: None)
    monkeypatch.setattr(m, "LOCAL_FALLBACK", "Z:/definitely-missing/node.exe")
    assert m.node_exe() == ""


def test_ci_workflow_keeps_setup_node():
    """R1118 的 setup-node 不能因为改解析器又被删掉——两条防线要同时存在。"""
    ci = (UNIT.parent.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "actions/setup-node" in ci
