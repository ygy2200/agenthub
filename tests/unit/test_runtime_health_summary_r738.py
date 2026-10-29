# -*- coding: utf-8 -*-
"""R738：runtime_health 摘要里的括号会误导。

实测：
    运行版本：落后 4 个文件（ops.py）——需重启 MCP 进程
括号里只点一个名字，很容易读成「只落后 ops.py」，而 `--full` 的明细还有 3 个。
`stale` 按 mtime 倒序，[0] 是**最近改动**的那个——那是有效信息，但不写清楚就成了歧义。

口径：单文件时照旧点名（括号无歧义）；多文件时括号要说清这是最近改动的那个、另有多少。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import ops as ops_mod  # noqa: E402


class _Core:
    base = r"C:\fake\mcp_server"
    basis = "content"
    source = "marker"
    started = "2026-10-29T18:18:00"

    def __init__(self, stale):
        self._stale = stale

    def runtime_staleness(self):
        return {"known": True, "count": len(self._stale), "stale": self._stale,
                "started": self.started, "base": self.base,
                "basis": self.basis, "source": self.source, "procs": []}


def _run(monkeypatch, stale):
    monkeypatch.setattr(ops_mod, "_import_core", lambda: _Core(stale))
    return ops_mod._runtime_health("")


def _stale(n):
    names = ["ops.py", "cli.py", "brain.py", "agenthub_mcp.py", "core.py"][:n]
    return [{"file": f, "mtime": "2026-10-29T18:02", "mtime_ts": 1.0, "reason": "changed"}
            for f in names]


def test_multi_file_summary_says_recent_and_count(monkeypatch):
    r = _run(monkeypatch, _stale(4))
    s = r["summary"]
    assert "落后 4 个文件" in s
    assert "最近改动：ops.py" in s and "另 3 个" in s
    assert "(ops.py)" not in s and "（ops.py）" not in s  # 旧的歧义写法不许回来


def test_single_file_keeps_plain_name(monkeypatch):
    r = _run(monkeypatch, _stale(1))
    assert r["summary"].count("ops.py") == 1
    assert "（ops.py）" in r["summary"]
    assert "另 0 个" not in r["summary"]


def test_detail_still_lists_files(monkeypatch):
    r = _run(monkeypatch, _stale(3))
    assert any(d.startswith("cli.py") for d in r["detail"])
