# -*- coding: utf-8 -*-
"""记忆钩子体检 R816/R821：注入链路要能被医生看见，而不是等用户再说「又不读记忆」。

客户端升级/重装会静默清掉 hooks 条目；脚本或 Python 入口被删则钩子静默空转。
两者必须报失败（给可执行修复）；有钩子零送达只说明这些天没开会话，只作信息行。
R821 起同时核对 SessionStart 简报与 UserPromptSubmit 回忆两条链路。
"""
import datetime
import json

import brain
import core
import ops


def _patch(monkeypatch, targets, hermes, tmp_path, with_scripts=True):
    monkeypatch.setattr(core, "HOOK_BRIEF_TARGETS", targets)
    monkeypatch.setattr(core, "HOOK_BRIEF_HERMES_TARGETS", hermes)
    d = tmp_path / "hooks"
    d.mkdir(exist_ok=True)
    if with_scripts:
        for n in (core.HOOK_BRIEF_NAME, core.HOOK_RECALL_NAME, core.HOOK_BRIEF_HERMES_SCRIPT,
                  core.HOOK_ADHERENCE_NAME):
            (d / n).write_text("// stub\n", encoding="utf-8")
    monkeypatch.setattr(core, "hook_script_path", lambda name: d / name)
    mcp = tmp_path / "mcp"
    mcp.mkdir(exist_ok=True)
    if with_scripts:
        for n in ("brain_brief.py", "brain_recall.py", "brain_adherence.py"):
            (mcp / n).write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(core, "MCP_SERVER_DIR", mcp)


def _cfg(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _both(script_brief=core.HOOK_BRIEF_NAME, script_recall=core.HOOK_RECALL_NAME):
    # R828：三条钩子（简报/回忆/回填）都齐才算「全在位」，否则体检会点名回填缺失。
    return {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": script_brief}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": script_recall}]}],
        "Stop": [{"hooks": [{"type": "command", "command": core.HOOK_ADHERENCE_NAME}]}],
    }}


def test_missing_hook_fails_with_actionable_fix(hub_root, tmp_path, monkeypatch):
    brain.init_db(hub_root)
    ok = _cfg(tmp_path, "ok.json", json.dumps(_both()))
    bad = _cfg(tmp_path, "bad.json", json.dumps({"hooks": {
        "SessionStart": [], "UserPromptSubmit": []}}))
    _patch(monkeypatch, {"qoder": str(ok), "dsh": str(bad)},
           (("hermes-cn", str(tmp_path / "none")),), tmp_path)
    r = ops.run_check("brief_hook_health", "", hub_root)
    assert r["ok"] is False
    assert "dsh" in r["summary"], r["summary"]
    assert "hook-install" in r["fix"], r["fix"]
    assert any("钩子丢失" in d and "dsh" in d for d in r["detail"]), r["detail"]


def test_missing_script_is_flagged_even_when_configs_look_fine(hub_root, tmp_path, monkeypatch):
    """配置里写着脚本名、脚本/Python 入口却没在盘上：钩子会静默空转，比配置丢失更隐蔽。"""
    brain.init_db(hub_root)
    ok = _cfg(tmp_path, "ok.json", json.dumps(_both()))
    _patch(monkeypatch, {"qoder": str(ok)}, (("hermes-cn", str(tmp_path / "none")),),
           tmp_path, with_scripts=False)
    r = ops.run_check("brief_hook_health", "", hub_root)
    assert r["ok"] is False, r
    assert "文件缺" in r["summary"], r["summary"]
    assert any("钩子文件缺失" in d and "brain_recall.py" in d for d in r["detail"]), r["detail"]


def test_all_in_place_is_ok_and_counts_deliveries(hub_root, tmp_path, monkeypatch):
    brain.init_db(hub_root)
    q = _cfg(tmp_path, "q.json", json.dumps(_both()))
    hpath = tmp_path / "h.yaml"
    hpath.write_text(f"hooks:\n  pre_llm_call:\n    - command: \"x {core.HOOK_BRIEF_HERMES_SCRIPT}\"\n",
                     encoding="utf-8")
    _patch(monkeypatch, {"qoder": str(q)}, (("hermes-cn", str(hpath)),), tmp_path)
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    with brain.db_conn(hub_root) as conn:
        brain._journal_append(conn, ts, "qoder", "brain_brief", "session", "", "x")
        brain._journal_append(conn, ts, "qoder", "brain_recall", "prompt", "", "x")
    r = ops.run_check("brief_hook_health", "", hub_root)
    assert r["ok"] is True, r
    assert "简报 2 家、回忆 2 家在位" in r["summary"], r["summary"]
    text = " ".join(r["detail"])
    assert "近 7 天简报送达：qoder 1" in text, text
    assert "近 7 天回忆送达：qoder 1" in text, text
    assert "零送达" in text and "hermes" in text, "钩子在位但没送达应作为信息行点名"


def test_missing_recall_hook_is_named(hub_root, tmp_path, monkeypatch):
    """只装了简报没装回忆：用户说的「还是要我提醒才查记忆」正是这条链路缺失。"""
    brain.init_db(hub_root)
    q = _cfg(tmp_path, "q.json", json.dumps({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": core.HOOK_BRIEF_NAME}]}]}}))
    _patch(monkeypatch, {"qoder": str(q)}, (("hermes-cn", str(tmp_path / "none")),), tmp_path)
    r = ops.run_check("brief_hook_health", "", hub_root)
    assert r["ok"] is False, r
    assert "回忆丢失 qoder" in r["summary"], r["summary"]
    assert any("钩子丢失" in d and "qoder" in d for d in r["detail"]), r["detail"]
