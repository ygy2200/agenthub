# -*- coding: utf-8 -*-
"""握手简报 R812：没有 SessionStart 钩子的 agent 也要在握手时看到大脑上下文。

病根：R811 的钩子只覆盖能挂 SessionStart 的客户端（Qoder/WorkBuddy/Claude）；
Codex/DSH/hermes 走的是纯 MCP，钩子装不上——它们过去只能靠 instructions 里的
建议去调工具（真实库 doctor：配置了 MCP 却零调用）。initialize 是所有 MCP 客户端
必经的握手点，把精简简报附在 instructions 末尾，才能覆盖全部接入方。
底线：静态规程必须原样在前（客户端/测试靠它路由）、简报超预算截断、坏根目录退回静态。
"""
import agenthub_mcp as m
import brain
import core


def _seed(root):
    brain.init_db(root)
    brain.add_record(root, "演示-握手简报", "qoder", "2026-10-30",
                     "2026-10-30（qoder）· 握手简报落地",
                     "目的：验证握手简报\n做了什么：initialize 附简报\n验证结果：单测")
    brain.add_memory(root, "握手也要带上下文", kind="lesson",
                     project="演示-握手简报", pinned=True)


def _init(root, name="Qoder CLI"):
    return m.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": m.PROTOCOL_VERSION,
                                        "clientInfo": {"name": name}}}, root)


def test_initialize_carries_static_rules_then_brief(hub_root):
    _seed(hub_root)
    res = _init(hub_root)
    ins = res["result"]["instructions"]
    assert ins.startswith(m.SERVER_INSTRUCTIONS), "静态规程必须原样在前"
    assert m.INIT_BRIEF_MARK in ins
    brief = ins.split(m.INIT_BRIEF_MARK, 1)[1]
    assert "握手简报落地" in brief, brief
    assert "MCP 握手" in brief and "SessionStart 钩子" not in brief, "来源口径不能写错"
    assert len(brief.strip()) <= m.INIT_BRIEF_MAX_CHARS


def test_initialize_journals_brief_for_adoption_metric(hub_root):
    _seed(hub_root)
    _init(hub_root)
    rows = [r for r in brain.journal_list(hub_root) if r["action"] == "brain_brief"]
    assert rows, "握手简报应留 brain_brief 流水，否则采纳度无法度量"
    assert rows[0]["target"] == "initialize"


def test_initialize_fails_open_on_bad_root(tmp_path):
    res = _init(str(tmp_path / "不存在"))
    assert res["result"]["instructions"] == m.SERVER_INSTRUCTIONS


def test_hub_brief_tool_journals_mcp_tool(hub_root):
    _seed(hub_root)
    m.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "hub_brief", "arguments": {"agent": "qoder"}}},
                     hub_root)
    rows = [r for r in brain.journal_list(hub_root) if r["action"] == "brain_brief"]
    assert any(r["target"] == "mcp_tool" for r in rows), rows
