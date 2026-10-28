# -*- coding: utf-8 -*-
r"""MCP 工具注解必须**诚实**（R263）。

规范默认值（MCP ToolAnnotations）：readOnlyHint=false / destructiveHint=**true** /
idempotentHint=false。两个真实缺陷：
  ① 追加型写工具（hub_log_work / hub_memory_write…）省略 destructiveHint，
     按规范默认等于声明「破坏性」——host 的确认 UX 与并发调度（Codex CLI 用
     readOnlyHint 决定并行）都会误判；
  ② 代理工具 hub_call_tool 能转调任意写工具（含归档/撤销），却被标 readOnlyHint=true，
     这是反向谎报。
守四件事：写工具必须显式声明 destructiveHint 且与高危集合一致；代理工具不可声明只读；
只读工具不带幂等/破坏性；派生集合关系成立。
"""
import agenthub_mcp as m


def _ann(name):
    return next(t for t in m.TOOLS if t["name"] == name)["annotations"]


def test_write_tools_declare_destructive_explicitly():
    for t in m.TOOLS:
        name = t["name"]
        if name in m.WRITE_TOOLS or name in m.PROXY_TOOLS:
            ann = t.get("annotations") or {}
            assert "destructiveHint" in ann, f"写工具 {name} 省略了 destructiveHint（默认 true）"
            assert not ann.get("readOnlyHint"), f"写/代理工具 {name} 被标只读"
            want = name in m.HIGH_BLAST_TOOLS or name in m.PROXY_TOOLS
            assert ann["destructiveHint"] is want, f"{name} destructiveHint 应为 {want}"


def test_proxy_tool_is_not_read_only():
    assert "hub_call_tool" in m.PROXY_TOOLS
    ann = _ann("hub_call_tool")
    assert not ann.get("readOnlyHint")
    assert ann.get("destructiveHint") is True, "代理可转调归档/撤销，必须按破坏性声明"


def test_additive_write_is_not_destructive():
    for name in ("hub_log_work", "hub_memory_write", "hub_report_error", "hub_event_write"):
        assert _ann(name).get("destructiveHint") is False, name


def test_read_only_tools_carry_no_write_hints():
    for t in m.TOOLS:
        name = t["name"]
        if name in m.WRITE_TOOLS or name in m.PROXY_TOOLS:
            continue
        ann = t.get("annotations") or {}
        assert ann.get("readOnlyHint") is True, name
        assert "destructiveHint" not in ann, name
        assert "idempotentHint" not in ann, name


def test_sets_are_disjoint():
    assert m.PROXY_TOOLS.isdisjoint(m.WRITE_TOOLS)
    assert m.HIGH_BLAST_TOOLS <= m.WRITE_TOOLS
    assert m.IDEMPOTENT_TOOLS <= m.WRITE_TOOLS
