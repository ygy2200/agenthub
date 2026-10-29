# -*- coding: utf-8 -*-
"""R705：引导内容没变时 refresh 不重写、不产备份、不动 mtime。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def test_inject_bootstrap_is_idempotent(tmp_path):
    import core

    root = tmp_path / "brain"
    root.mkdir()
    core.init_hub(str(root))
    instr = tmp_path / "AGENTS.md"
    instr.write_text("# 我的规则\n", encoding="utf-8")

    assert core.inject_bootstrap(str(instr), str(root), "user") == ""
    first = instr.read_text(encoding="utf-8")
    before = {p.name for p in tmp_path.iterdir()}
    mtime = instr.stat().st_mtime_ns

    time.sleep(0.01)
    assert core.inject_bootstrap(str(instr), str(root), "user") == ""
    assert instr.read_text(encoding="utf-8") == first
    assert instr.stat().st_mtime_ns == mtime, "内容未变却重写了引导文件"
    assert {p.name for p in tmp_path.iterdir()} == before, "内容未变却产生了备份文件"


def test_refresh_reports_unchanged(tmp_path, monkeypatch, capsys):
    import cli
    import core

    root = tmp_path / "brain"
    root.mkdir()
    core.init_hub(str(root))
    instr = tmp_path / "AGENTS.md"
    assert core.inject_bootstrap(str(instr), str(root), "user") == ""
    monkeypatch.setattr(core, "load_agent_adapters",
                        lambda r: [{"agent": "x", "instr_path": str(instr)}])

    rc = cli.cmd_refresh(str(root))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "已是最新" in out and "0 个更新" in out, out
