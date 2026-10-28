# -*- coding: utf-8 -*-
"""UI 体检的失败证据（R220）：摘要只回一个 FAIL 等于把排查推回给人重跑。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def _audit():
    p = str(ROOT / "tools")
    if p not in sys.path:
        sys.path.insert(0, p)
    import tools.ui_audit as ua
    ua.results.clear()
    return ua


def test_failure_keeps_tail_output():
    ua = _audit()
    ua.run("示例失败", [sys.executable, "-c",
                       "import sys; print('第一行'); print('断言：X 应为 1', file=sys.stderr); "
                       "sys.exit(1)"])
    _, ok, _, detail, evidence = ua.results[-1]
    assert not ok and detail == ""
    assert any("断言：X 应为 1" in ln for ln in evidence), evidence


def test_success_parses_and_has_no_evidence():
    ua = _audit()
    ua.run("示例通过", [sys.executable, "-c", "print('ok 3 页')"],
           lambda out: "3 页一致" if "3 页" in out else "")
    _, ok, _, detail, evidence = ua.results[-1]
    assert ok and detail == "3 页一致" and evidence == []


def test_timeout_is_reported_not_raised(monkeypatch):
    ua = _audit()

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=600)

    monkeypatch.setattr(ua.subprocess, "run", _boom)
    ua.run("示例超时", [sys.executable, "-c", "pass"])
    _, ok, _, detail, evidence = ua.results[-1]
    assert not ok and "超时" in detail and evidence
