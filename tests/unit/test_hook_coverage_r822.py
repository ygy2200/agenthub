# -*- coding: utf-8 -*-
"""R822：没有钩子出口的客户端要被点名，而不是从「N 家在位」里静默消失。

钩子只覆盖 HOOK_BRIEF_TARGETS / HOOK_BRIEF_HERMES_TARGETS 登记过的那几家。本机 7 家
接入里 ZCode 不提供 SessionStart/UserPromptSubmit 事件，旧实现在体检与接入页里只数
6 家——用户看到「6 家全部在位」会以为第 7 家也行，实际它只能靠 MCP 握手带一次简报。
修之前：hook_unmanaged_agents 不存在，体检明细里一个字都不提 ZCode。
"""
import json

import brain
import core
import ops


def _patch(monkeypatch, targets, hermes, tmp_path):
    monkeypatch.setattr(core, "HOOK_BRIEF_TARGETS", targets)
    monkeypatch.setattr(core, "HOOK_BRIEF_HERMES_TARGETS", hermes)
    d = tmp_path / "hooks"
    d.mkdir(exist_ok=True)
    for n in (core.HOOK_BRIEF_NAME, core.HOOK_RECALL_NAME, core.HOOK_BRIEF_HERMES_SCRIPT,
              core.HOOK_ADHERENCE_NAME):
        (d / n).write_text("// stub\n", encoding="utf-8")
    monkeypatch.setattr(core, "hook_script_path", lambda name: d / name)
    mcp = tmp_path / "mcp"
    mcp.mkdir(exist_ok=True)
    for n in ("brain_brief.py", "brain_recall.py", "brain_adherence.py"):
        (mcp / n).write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(core, "MCP_SERVER_DIR", mcp)
    return d


def _both():
    return {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": core.HOOK_BRIEF_NAME}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": core.HOOK_RECALL_NAME}]}],
        "Stop": [{"hooks": [{"type": "command", "command": core.HOOK_ADHERENCE_NAME}]}],
    }}


def test_zcode_has_no_hook_outlet_and_is_listed(hub_root):
    """R824 取代本测试旧前提（「ZCode 不提供 SessionStart/UserPromptSubmit」）——
    ZCode 官方支持这七个事件（diagnosing-hooks 文档），且其 Stop 钩子 10-25 前一直在跑。
    ZCode 已登记进 HOOK_BRIEF_TARGETS，不再被点名；点名机制本身由下方 monkeypatch 测试覆盖。"""
    unmanaged = core.hook_unmanaged_agents(hub_root)
    assert "ZCode" not in unmanaged, unmanaged
    for managed in ("WorkBuddy", "Qoder", "hermes"):
        assert managed not in unmanaged, f"{managed} 有钩子出口，不该算未纳管：{unmanaged}"


def test_managed_keys_merge_hermes_yaml_outlet():
    """hermes 的出口是 YAML 的 pre_llm_call，不能因为不在 JSON 表里就被当成未纳管。"""
    keys = core.hook_managed_keys()
    assert "hermes" in keys and "qoder" in keys, keys


def test_doctor_detail_names_unmanaged_client(hub_root, tmp_path, monkeypatch):
    """体检通过时也要说清「还有一家没这个口子」，摘要带上家数。"""
    brain.init_db(hub_root)
    q = tmp_path / "q.json"
    q.write_text(json.dumps(_both()), encoding="utf-8")
    hpath = tmp_path / "h.yaml"
    hpath.write_text(f"hooks:\n  pre_llm_call:\n    - command: \"x {core.HOOK_BRIEF_HERMES_SCRIPT}\"\n",
                     encoding="utf-8")
    _patch(monkeypatch, {"qoder": str(q)}, (("hermes-cn", str(hpath)),), tmp_path)
    r = ops.run_check("brief_hook_health", "", hub_root)
    assert r["ok"] is True, r
    assert "家无钩子出口" in r["summary"], r["summary"]
    line = next((d for d in r["detail"] if d.startswith("无记忆钩子出口")), "")
    assert "ZCode" in line, r["detail"]
    assert "不是故障" in line, "未纳管不是坏，文案不能让人去按「钩子丢失」修"


def test_unmanaged_never_counts_as_a_failure(hub_root, tmp_path, monkeypatch):
    """点名的目的是说清楚，不是报警：未纳管不进 issues、不把体检弄红。"""
    brain.init_db(hub_root)
    q = tmp_path / "q.json"
    q.write_text(json.dumps(_both()), encoding="utf-8")
    _patch(monkeypatch, {"qoder": str(q)}, (("hermes-cn", str(tmp_path / "none")),), tmp_path)
    r = ops.run_check("brief_hook_health", "", hub_root)
    assert r["ok"] is True, r
