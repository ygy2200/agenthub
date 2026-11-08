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
                  core.HOOK_ADHERENCE_NAME, core.HOOK_ERROR_RECALL_NAME):
            (d / n).write_text("// stub\n", encoding="utf-8")
    monkeypatch.setattr(core, "hook_script_path", lambda name: d / name)
    mcp = tmp_path / "mcp"
    mcp.mkdir(exist_ok=True)
    if with_scripts:
        for n in ("brain_brief.py", "brain_recall.py", "brain_adherence.py",
                  "brain_error_recall.py"):
            (mcp / n).write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(core, "MCP_SERVER_DIR", mcp)


def _cfg(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _both(script_brief=core.HOOK_BRIEF_NAME, script_recall=core.HOOK_RECALL_NAME):
    # R833：四条钩子（简报/回忆/回填/报错）都齐才算「全在位」，否则体检会点名缺失。
    return {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": script_brief}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": script_recall}]}],
        "Stop": [{"hooks": [{"type": "command", "command": core.HOOK_ADHERENCE_NAME}]}],
        "PostToolUseFailure": [{"hooks": [{"type": "command", "command": core.HOOK_ERROR_RECALL_NAME}]}],
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
        brain._journal_append(conn, ts, "qoder", "brain_error_recall", "error", "", "x")
    r = ops.run_check("brief_hook_health", "", hub_root)
    assert r["ok"] is True, r
    assert "简报 2 家、回忆 2 家、报错回忆 1 家在位" in r["summary"], r["summary"]
    text = " ".join(r["detail"])
    assert "近 7 天简报送达：qoder 1" in text, text
    assert "近 7 天回忆送达：qoder 1" in text, text
    assert "近 7 天报错回忆送达：qoder 1" in text, text
    assert "零送达" in text and "hermes" in text, "钩子在位但没送达应作为信息行点名"


def test_missing_error_hook_is_named_but_not_red(hub_root, tmp_path, monkeypatch):
    """R833：机器还没升第 4 条钩子时，明细要点名 + 给修复命令，但不把体检刷成红。

    红是给「装坏了」的，不是给「还没装新的」的——否则每台未升级机器天天红，
    用户很快就不看体检了（同 R828 回填的口径）。
    """
    brain.init_db(hub_root)
    cfg = _both()
    del cfg["hooks"]["PostToolUseFailure"]
    q = _cfg(tmp_path, "q.json", json.dumps(cfg))
    _patch(monkeypatch, {"qoder": str(q)}, (("hermes-cn", str(tmp_path / "none")),), tmp_path)
    r = ops.run_check("brief_hook_health", "", hub_root)
    assert r["ok"] is True, r
    assert any("报错回忆钩子丢失" in d and "qoder" in d for d in r["detail"]), r["detail"]
    assert any("--events error --apply" in d for d in r["detail"]), r["detail"]


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


def test_error_hook_without_posttooluse_is_named(hub_root, tmp_path, monkeypatch):
    """R987：报错钩子只挂 PostToolUseFailure，成功路径的两段兜底就是死代码。

    兜底（R971 Python traceback / R986 Node 异常）要处理的正是「宿主不判定失败」的
    exit=0 场景——只挂失败事件等于它永远不会被调用。真库实测：日志里 27 条兜底记录
    全是测试 sid，真实会话 0 条；这台机器上 6 家 agent 里 5 家都缺这个出口。
    """
    brain.init_db(hub_root)
    cfg = _cfg(tmp_path, "only-failure.json", json.dumps(_both()))
    _patch(monkeypatch, {"qoder": str(cfg)}, (("hermes-cn", str(tmp_path / "none")),),
           tmp_path)
    r = ops._brief_hook_health(hub_root)
    assert any("只挂了失败事件" in d and "qoder" in d for d in r["detail"]), r["detail"]


def test_error_hook_with_posttooluse_is_not_named(hub_root, tmp_path, monkeypatch):
    """反证：补上 PostToolUse 后不再点名，否则这条检查会变成永远消不掉的噪音。"""
    brain.init_db(hub_root)
    data = _both()
    data["hooks"]["PostToolUse"] = [
        {"hooks": [{"type": "command", "command": core.HOOK_ERROR_RECALL_NAME}]}]
    cfg = _cfg(tmp_path, "both-events.json", json.dumps(data))
    _patch(monkeypatch, {"qoder": str(cfg)}, (("hermes-cn", str(tmp_path / "none")),),
           tmp_path)
    r = ops._brief_hook_health(hub_root)
    assert not any("只挂了失败事件" in d for d in r["detail"]), r["detail"]


def test_script_mounted_without_its_event_is_named(hub_root, tmp_path, monkeypatch):
    """R989：脚本挂在配置里、它要的事件却没挂 → 对应分支永不触发。

    R987 的通用化：不只 error_recall 有「挂了但不是那个事件」的问题。这条把
    core.HOOK_EVENT_CONTRACT 钉住——以后客户端升级只留一半 hooks，体检会点名。
    """
    brain.init_db(hub_root)
    data = _both()
    data["hooks"].pop("SessionStart")            # 简报事件没了
    data["hooks"]["Stop"][0]["hooks"].append(    # 但脚本名还在配置文本里
        {"type": "command", "command": core.HOOK_BRIEF_NAME})
    cfg = _cfg(tmp_path, "no-session.json", json.dumps(data))
    _patch(monkeypatch, {"qoder": str(cfg)}, (("hermes-cn", str(tmp_path / "none")),),
           tmp_path)
    r = ops._brief_hook_health(hub_root)
    assert any("钩子事件不齐" in d and "brain_inject.mjs" in d and "SessionStart" in d
               for d in r["detail"]), r["detail"]
