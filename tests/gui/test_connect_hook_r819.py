# -*- coding: utf-8 -*-
"""gui 层：接入中心摆出各家简报钩子状态，缺口可一键补齐（R819）。

MCP 接入只保证「能用」，钩子才保证「不提醒也自动收到上下文」。此前这个差别只藏在
doctor 和 CLI 里，用户在接入页看不到「谁的新会话不会自动带记忆」。
"""
import json

from PySide6.QtWidgets import QWidget

from helpers import track_window


def _cfg(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def _hooked(tmp_path, name="hooked.json"):
    return _cfg(tmp_path, name, {"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "node brain_inject.mjs --agent x"}]}]}})


def _plain(tmp_path, name="plain.json"):
    return _cfg(tmp_path, name, {"hooks": {}})


def _build(qapp, hub_root, monkeypatch, targets, hermes):
    import core
    from pages.connect_page import ConnectPage

    monkeypatch.setattr(core, "load_agent_adapters", lambda root="": [
        {"agent": "a", "mcp": None, "instr_path": ""}])
    monkeypatch.setattr(core, "deploy_status", lambda *a, **k: {
        "python": "D:/python.exe", "server": "/fake/server", "state": "ready",
        "diff": [], "absent": [], "same": 11})
    monkeypatch.setattr(core, "HOOK_BRIEF_TARGETS", targets)
    monkeypatch.setattr(core, "HOOK_BRIEF_HERMES_TARGETS", hermes)
    win = QWidget()
    win.root = str(hub_root)
    track_window(win)
    page = ConnectPage(win, win)
    page._bootTimer.stop()
    page.refresh_status()
    return win, page


def test_unwired_agent_is_named_and_button_enabled(qapp, hub_root, tmp_path, monkeypatch):
    win, page = _build(
        qapp, hub_root, monkeypatch,
        {"qoder": str(_hooked(tmp_path)), "dsh": str(_plain(tmp_path))},
        (("hermes-cn", str(tmp_path / "none.yaml")),))
    try:
        text = page.hookLabel.text()
        assert "dsh 未挂" in text, text
        assert "qoder" not in text, "已在位的不该被点名成缺口"
        assert "本机无配置" in text and "hermes" in text
        assert page.hookBtn.isEnabled()
        assert "dsh" in page.hookLabel.toolTip()
    finally:
        win.close()


def test_all_in_place_says_automatic_context(qapp, hub_root, tmp_path, monkeypatch):
    h = tmp_path / "hermes.yaml"
    h.write_text("hooks:\n  pre_llm_call:\n    - command: 'node brain_inject_hermes.mjs'\n",
                 encoding="utf-8")
    win, page = _build(qapp, hub_root, monkeypatch,
                       {"qoder": str(_hooked(tmp_path))}, (("hermes-cn", str(h)),))
    try:
        text = page.hookLabel.text()
        assert "全部在位" in text and "不需要再提醒" in text, text
        assert not page.hookBtn.isEnabled(), "没有缺口时按钮不该可点"
    finally:
        win.close()


def test_install_only_touches_unwired_agents(qapp, hub_root, tmp_path, monkeypatch):
    import core

    calls = []
    real_json = core.install_brief_hook
    real_yaml = core.install_hermes_brief_hook

    def fake_json(agent, apply=False, **kw):
        if not apply:
            return real_json(agent, apply=False, **kw)
        calls.append(("json", agent, apply))
        return {"agent": agent, "status": "added", "path": "x"}

    def fake_yaml(agent="hermes-cn", apply=False, **kw):
        if not apply:
            return real_yaml(agent, apply=False, **kw)
        calls.append(("yaml", agent, apply))
        return {"agent": agent, "status": "added", "path": "x"}

    monkeypatch.setattr(core, "install_brief_hook", fake_json)
    monkeypatch.setattr(core, "install_hermes_brief_hook", fake_yaml)
    win, page = _build(
        qapp, hub_root, monkeypatch,
        {"qoder": str(_hooked(tmp_path)), "dsh": str(_plain(tmp_path))},
        (("hermes-cn", str(tmp_path / "none.yaml")),))
    try:
        assert page.hookBtn.isEnabled()
        page.do_install_hooks()
        assert calls == [("json", "dsh", True)], calls
    finally:
        win.close()
