# -*- coding: utf-8 -*-
"""e2e 层：agent 工作闭环（BDD 场景）。

场景用真实协议消息驱动（handle_message / 真实 stdio 子进程），不直接调 brain.*——
那是 unit 层的事。给定空大脑根目录，模拟 agent 客户端完成：握手 → 记录 → 检索 → 记忆。
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _rpc(root, mid, method, params=None):
    import agenthub_mcp
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    resp = agenthub_mcp.handle_message(msg, root)
    assert resp is not None and resp.get("id") == mid
    return resp


def _tool(root, mid, name, args):
    resp = _rpc(root, mid, "tools/call", {"name": name, "arguments": args})
    result = resp.get("result") or {}
    text = (result.get("content") or [{}])[0].get("text", "")
    return text, bool(result.get("isError"))


def test_scenario_work_log_and_search(hub_root):
    """Given 空大脑 / When 写工作记录 / Then 检索命中、项目详情可见。"""
    import brain
    assert brain.ensure_schema(hub_root) == ""

    resp = _rpc(hub_root, 1, "initialize", {"protocolVersion": "2024-11-05"})
    assert resp["result"]["serverInfo"]["name"], "initialize 应返回 serverInfo"

    text, err = _tool(hub_root, 2, "hub_log_work", {
        "project": "E2E-场景", "agent": "zcode",
        "content": "目的：验证端到端闭环\n做了什么：写一条记录\n验证结果：本断言\n如何回滚：无"})
    assert not err, text
    assert "已记入大脑" in text and "records#" in text, text

    text2, err2 = _tool(hub_root, 3, "hub_search", {"keyword": "E2E-场景"})
    assert not err2 and "E2E-场景" in text2, text2

    text3, err3 = _tool(hub_root, 4, "hub_get_project", {"project": "E2E-场景"})
    assert not err3 and "E2E-场景" in text3, text3


def test_scenario_memory_write_and_read(hub_root):
    """Given 有条目的库 / When 写记忆并检索 / Then 命中。"""
    import brain
    brain.ensure_schema(hub_root)
    t1, e1 = _tool(hub_root, 1, "hub_memory_write", {
        "content": "E2E 记忆样本：端到端链路验证用", "kind": "note", "agent": "zcode"})
    assert not e1, t1
    t2, e2 = _tool(hub_root, 2, "hub_memory_read", {"query": "E2E 记忆样本"})
    assert not e2 and "E2E 记忆样本" in t2, t2


def test_scenario_stdio_server_real_process(hub_root):
    """真实进程：拉起 agenthub_mcp.py，走 stdio 完成 initialize / tools-list / 一次调用。"""
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "agenthub_mcp.py"), "--agent", "zcode", hub_root],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8")
    try:
        reqs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "hub_status", "arguments": {}}},
        ]
        proc.stdin.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reqs))
        proc.stdin.flush()
        lines = []
        while len(lines) < 3:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(json.loads(line))
        assert len(lines) == 3, f"响应不足：{lines}"
        assert lines[0]["result"]["protocolVersion"] == "2024-11-05"
        tools = lines[1]["result"]["tools"]
        assert len(tools) >= 10, f"工具数异常：{len(tools)}"
        text = lines[2]["result"]["content"][0]["text"]
        assert text and len(text) > 10, f"hub_status 返回异常：{text!r}"
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
