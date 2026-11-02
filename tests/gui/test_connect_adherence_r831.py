# -*- coding: utf-8 -*-
"""gui 层：接入中心把「采纳回填」也摆出来（R831）。

为什么改：R828 给三家 JSON 客户端挂了 Stop 回填钩子，CLI/doctor 早就会逐家报
「简报+回忆+回填」三态，桌面接入页却只认前两条——只缺回填的那家，桌面显示
「全部在位」，用户既看不到注入账本断了，点「安装简报钩子」也什么都不会发生
（do_install_hooks 只处理 session/prompt 两态）。

本文件钉住三件事：
  1. 只缺回填 → 点名，并说清后果（账本停在「不知道有没有用」）；
  2. 三条齐全 → 文案里带「回填」，按钮不可点；
  3. 只缺回填的那家，点按钮真的会去装（旧代码在这里是空转）。
"""
import json

from PySide6.QtWidgets import QWidget

from helpers import track_window


def _cfg(tmp_path, name, events):
    p = tmp_path / name
    p.write_text(json.dumps({"hooks": events}, ensure_ascii=False), encoding="utf-8")
    return p


def _all_three(tmp_path, name="full.json"):
    # R833 起「全部在位」是四条：少报错回忆，接入页会照样点名这家是缺口，
    # 这条用例守的「齐全时按钮不可点」就会假红
    return _cfg(tmp_path, name, {
        "SessionStart": [{"hooks": [{"type": "command", "command": "node brain_inject.mjs"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "node brain_recall.mjs"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "node brain_adherence.mjs"}]}],
        "PostToolUseFailure": [{"hooks": [{"type": "command",
                                           "command": "node brain_error_recall.mjs"}]}],
    })


def _no_backfill(tmp_path, name="nobackfill.json"):
    return _cfg(tmp_path, name, {
        "SessionStart": [{"hooks": [{"type": "command", "command": "node brain_inject.mjs"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "node brain_recall.mjs"}]}],
    })


def _build(qapp, hub_root, monkeypatch, targets, tmp_path):
    import core
    from pages.connect_page import ConnectPage

    monkeypatch.setattr(core, "load_agent_adapters", lambda root="": [
        {"agent": "a", "mcp": None, "instr_path": ""}])
    monkeypatch.setattr(core, "deploy_status", lambda *a, **k: {
        "python": "D:/python.exe", "server": "/fake/server", "state": "ready",
        "diff": [], "absent": [], "same": 11})
    monkeypatch.setattr(core, "HOOK_BRIEF_TARGETS", targets)
    monkeypatch.setattr(core, "HOOK_BRIEF_HERMES_TARGETS",
                        (("hermes-cn", str(tmp_path / "none.yaml")),))
    hooks = tmp_path / "hooks"
    hooks.mkdir(exist_ok=True)
    for n in ("brain_inject.mjs", "brain_recall.mjs", "brain_adherence.mjs",
              "brain_error_recall.mjs"):
        (hooks / n).write_text("// stub\n", encoding="utf-8")
    monkeypatch.setattr(core, "hook_script_path", lambda name: hooks / name)
    win = QWidget()
    win.root = str(hub_root)
    track_window(win)
    page = ConnectPage(win, win)
    page._bootTimer.stop()
    page.refresh_status()
    return win, page


def test_missing_backfill_is_named_with_consequence(qapp, hub_root, tmp_path, monkeypatch):
    win, page = _build(qapp, hub_root, monkeypatch,
                       {"qoder": str(_no_backfill(tmp_path))}, tmp_path)
    try:
        text = page.hookLabel.text()
        assert "qoder" in text and "采纳回填" in text, text
        assert "不知道有没有用" in text, "光点名不够，要说清断了会怎样：" + text
        assert "全部在位" not in text, "缺一条就不是全部在位：" + text
        assert page.hookBtn.isEnabled(), "缺回填时按钮必须可点（否则用户没有补齐的入口）"
        assert "回填 unwired" in page.hookLabel.toolTip(), page.hookLabel.toolTip()
    finally:
        win.close()


def test_all_three_in_place_mentions_backfill(qapp, hub_root, tmp_path, monkeypatch):
    win, page = _build(qapp, hub_root, monkeypatch,
                       {"qoder": str(_all_three(tmp_path))}, tmp_path)
    try:
        text = page.hookLabel.text()
        assert "全部在位" in text and "回填" in text, text
        assert not page.hookBtn.isEnabled(), "三条都齐时按钮不该可点"
    finally:
        win.close()


def test_install_button_covers_backfill_only_gap(qapp, hub_root, tmp_path, monkeypatch):
    """旧实现这里空转：session/prompt 都在位 → continue，回填永远补不上。"""
    import core

    calls = []

    def fake_install(agent, apply=False, **kw):
        if not apply:
            return {"session": {"status": "already"}, "prompt": {"status": "already"},
                    "answer": {"status": "would_add"}}
        calls.append(agent)
        return {"session": {"status": "already"}, "prompt": {"status": "already"},
                "answer": {"status": "added"}}

    monkeypatch.setattr(core, "install_memory_hooks", fake_install)
    win, page = _build(qapp, hub_root, monkeypatch,
                       {"qoder": str(_no_backfill(tmp_path))}, tmp_path)
    try:
        page.do_install_hooks()
        assert calls == ["qoder"], calls
    finally:
        win.close()
