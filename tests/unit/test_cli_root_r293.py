# -*- coding: utf-8 -*-
r"""`agenthub root` 与状态页根目录告警（R293）。

此前 CLI 没有切换根目录的能力：`init --root 新目录` 之后，agent 的 MCP 进程仍跟随
全局配置写旧大脑，而 `status --root 新目录` 会把接入状态显示成全绿——假阳性。
"""
import core
import cli


def _mk_brain(path):
    (path / "_hub").mkdir(parents=True)
    (path / "_hub" / "brain.db").write_bytes(b"")


def test_root_show_when_unconfigured(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_FILE", tmp_path / "config.json")
    rc = cli.main(["root"])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "尚未配置" in out and "init" in out, out


def test_root_set_and_show(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_FILE", tmp_path / "config.json")
    hub = tmp_path / "brain"
    _mk_brain(hub)
    rc = cli.main(["root", "--set", str(hub)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert core.load_config()["root"] == str(hub)
    rc = cli.main(["root"])
    out = capsys.readouterr().out
    assert rc == 0 and str(hub) in out, out


def test_root_set_rejects_non_brain_and_missing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_FILE", tmp_path / "config.json")
    plain = tmp_path / "plain"
    plain.mkdir()
    rc = cli.main(["root", "--set", str(plain)])
    out = capsys.readouterr().out
    assert rc == 2, out
    assert "还不是大脑" in out and "init" in out, out
    rc = cli.main(["root", "--set", str(tmp_path / "nope")])
    out = capsys.readouterr().out
    assert rc == 2 and "目录不存在" in out, out


def test_status_warns_when_root_differs_from_config(tmp_path, capsys, monkeypatch):
    cfg_brain = tmp_path / "cfg_brain"
    other = tmp_path / "other"
    cfg_brain.mkdir()
    other.mkdir()
    monkeypatch.setattr(core, "load_config", lambda: {"root": str(cfg_brain)})
    monkeypatch.setattr(core, "agent_bootstrap_status", lambda root: [{
        "agent": "zcode", "channel": "instr_file", "instr_path": "x",
        "boot": True, "boot_version": core.BOOTSTRAP_VERSION, "boot_stale": False,
        "mcp_path": "", "layout": "", "profile": "", "mcp": True}])
    rc = cli.main(["status", "--root", str(other)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "⚠" in out and str(other) in out and str(cfg_brain) in out, out
    assert "root --set" in out, out


def test_status_silent_when_root_matches_config(tmp_path, capsys, monkeypatch):
    hub = tmp_path / "brain"
    hub.mkdir()
    monkeypatch.setattr(core, "load_config", lambda: {"root": str(hub)})
    monkeypatch.setattr(core, "agent_bootstrap_status", lambda root: [])
    rc = cli.main(["status", "--root", str(hub)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "⚠" not in out, out
