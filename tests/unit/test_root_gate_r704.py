# -*- coding: utf-8 -*-
"""R704：不依赖脑库的命令（discover/tools）不被根目录闸门拦住；错路径给可行动说明。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def test_tools_runs_without_configured_root(capsys, monkeypatch):
    import cli

    monkeypatch.setattr(cli, "_root", lambda arg="": "")
    rc = cli.main(["tools"])
    out = capsys.readouterr().out
    assert rc == 0 and "hub_search" in out, (rc, out[:200])


def test_discover_runs_without_root(capsys, monkeypatch):
    import cli
    import core

    monkeypatch.setattr(cli, "_root", lambda arg="": "")
    monkeypatch.setattr(core, "discover_agent_configs", lambda root: [])
    monkeypatch.setattr(core, "load_agent_adapters", lambda root: [])
    rc = cli.main(["discover"])
    out = capsys.readouterr().out
    assert rc == 0 and "扫描结果" in out, (rc, out)


def test_db_command_on_plain_directory_explains_init(tmp_path, capsys):
    import cli

    rc = cli.main(["tasks", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 2 and "还不是 AgentHub 大脑" in out and "init --root" in out, out


def test_db_command_on_missing_path_explains_typo(tmp_path, capsys):
    import cli

    missing = tmp_path / "typo"
    rc = cli.main(["tasks", "--root", str(missing)])
    out = capsys.readouterr().out
    assert rc == 2 and "不存在" in out and "路径打错" in out, out
