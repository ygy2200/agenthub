# -*- coding: utf-8 -*-
"""简报钩子体检 R816：注入链路要能被医生看见，而不是等用户再说「又不读记忆」。

客户端升级/重装会静默清掉 hooks 条目；钩子在但没送达，只说明这些天没开会话。
两者必须分开报：丢失=失败（给可执行修复），零送达=信息行（不制造假红）。
"""
import datetime
import json

import brain
import core
import ops


def _patch(monkeypatch, targets, hermes):
    monkeypatch.setattr(core, "HOOK_BRIEF_TARGETS", targets)
    monkeypatch.setattr(core, "HOOK_BRIEF_HERMES_TARGETS", hermes)


def _cfg(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_missing_hook_fails_with_actionable_fix(hub_root, tmp_path, monkeypatch):
    brain.init_db(hub_root)
    ok = _cfg(tmp_path, "ok.json", json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": core.HOOK_BRIEF_NAME}]}]}}))
    bad = _cfg(tmp_path, "bad.json", json.dumps({"hooks": {"SessionStart": []}}))
    _patch(monkeypatch, {"qoder": str(ok), "dsh": str(bad)}, (("hermes-cn", str(tmp_path / "none")),))
    r = ops.run_check("brief_hook_health", "", hub_root)
    assert r["ok"] is False
    assert "dsh" in r["summary"], r["summary"]
    assert "hook-install" in r["fix"], r["fix"]
    assert any("钩子丢失" in d and "dsh" in d for d in r["detail"]), r["detail"]


def test_all_in_place_is_ok_and_counts_deliveries(hub_root, tmp_path, monkeypatch):
    brain.init_db(hub_root)
    _cfg(tmp_path, "q.json", json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": core.HOOK_BRIEF_NAME}]}]}}))
    _cfg(tmp_path, "h.yaml", "hooks:\n  pre_llm_call:\n    - command: \"x\"\n")
    hpath = tmp_path / "h.yaml"
    hpath.write_text(f"hooks:\n  pre_llm_call:\n    - command: \"x {core.HOOK_BRIEF_HERMES_SCRIPT}\"\n",
                     encoding="utf-8")
    _patch(monkeypatch, {"qoder": str(tmp_path / "q.json")}, (("hermes-cn", str(hpath)),))
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    with brain.db_conn(hub_root) as conn:
        brain._journal_append(conn, ts, "qoder", "brain_brief", "session", "", "x")
    r = ops.run_check("brief_hook_health", "", hub_root)
    assert r["ok"] is True, r
    assert "2 家在位" in r["summary"], r["summary"]
    text = " ".join(r["detail"])
    assert "近 7 天送达：qoder 1" in text, text
    assert "零送达" in text and "hermes" in text, "钩子在位但没送达应作为信息行点名"
