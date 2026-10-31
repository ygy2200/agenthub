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


def _patch(monkeypatch, targets, hermes, tmp_path=None, with_scripts=True):
    monkeypatch.setattr(core, "HOOK_BRIEF_TARGETS", targets)
    monkeypatch.setattr(core, "HOOK_BRIEF_HERMES_TARGETS", hermes)
    if tmp_path is not None and with_scripts:  # 脚本本身单独测，这里先保证「脚本在盘上」
        d = tmp_path / "hooks"
        d.mkdir(exist_ok=True)
        for n in (core.HOOK_BRIEF_NAME, core.HOOK_BRIEF_HERMES_SCRIPT):
            (d / n).write_text("// stub\n", encoding="utf-8")
        monkeypatch.setattr(core, "hook_brief_script_path", lambda: d / "x")


def _cfg(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_missing_hook_fails_with_actionable_fix(hub_root, tmp_path, monkeypatch):
    brain.init_db(hub_root)
    ok = _cfg(tmp_path, "ok.json", json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": core.HOOK_BRIEF_NAME}]}]}}))
    bad = _cfg(tmp_path, "bad.json", json.dumps({"hooks": {"SessionStart": []}}))
    _patch(monkeypatch, {"qoder": str(ok), "dsh": str(bad)},
           (("hermes-cn", str(tmp_path / "none")),), tmp_path)
    r = ops.run_check("brief_hook_health", "", hub_root)
    assert r["ok"] is False
    assert "dsh" in r["summary"], r["summary"]
    assert "hook-install" in r["fix"], r["fix"]
    assert any("钩子丢失" in d and "dsh" in d for d in r["detail"]), r["detail"]


def test_missing_script_is_flagged_even_when_configs_look_fine(hub_root, tmp_path, monkeypatch):
    """配置里写着脚本名、脚本却没在盘上：钩子会静默空转，比配置丢失更隐蔽。"""
    brain.init_db(hub_root)
    ok = _cfg(tmp_path, "ok.json", json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": core.HOOK_BRIEF_NAME}]}]}}))
    _patch(monkeypatch, {"qoder": str(ok)}, (("hermes-cn", str(tmp_path / "none")),), tmp_path,
           with_scripts=False)
    monkeypatch.setattr(core, "hook_brief_script_path", lambda: tmp_path / "hooks" / "x")
    r = ops.run_check("brief_hook_health", "", hub_root)
    assert r["ok"] is False, r
    assert "脚本缺" in r["summary"], r["summary"]
    assert any("钩子脚本缺失" in d for d in r["detail"]), r["detail"]


def test_all_in_place_is_ok_and_counts_deliveries(hub_root, tmp_path, monkeypatch):
    brain.init_db(hub_root)
    _cfg(tmp_path, "q.json", json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": core.HOOK_BRIEF_NAME}]}]}}))
    _cfg(tmp_path, "h.yaml", "hooks:\n  pre_llm_call:\n    - command: \"x\"\n")
    hpath = tmp_path / "h.yaml"
    hpath.write_text(f"hooks:\n  pre_llm_call:\n    - command: \"x {core.HOOK_BRIEF_HERMES_SCRIPT}\"\n",
                     encoding="utf-8")
    _patch(monkeypatch, {"qoder": str(tmp_path / "q.json")},
           (("hermes-cn", str(hpath)),), tmp_path)
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    with brain.db_conn(hub_root) as conn:
        brain._journal_append(conn, ts, "qoder", "brain_brief", "session", "", "x")
    r = ops.run_check("brief_hook_health", "", hub_root)
    assert r["ok"] is True, r
    assert "2 家在位" in r["summary"], r["summary"]
    text = " ".join(r["detail"])
    assert "近 7 天送达：qoder 1" in text, text
    assert "零送达" in text and "hermes" in text, "钩子在位但没送达应作为信息行点名"
