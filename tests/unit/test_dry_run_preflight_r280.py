# -*- coding: utf-8 -*-
r"""dry_run 预演必须做入参预检（R280）。

旧行为：协议层与 hub_call_tool 的 dry_run 分支都在 call_tool 之前返回「预演通过」，
缺必填、优先级传 "abc" 也照样绿——模型把预演当写前验证，真正的错误留到正式执行才爆。
现在预演先跑与真实执行同一套必填/整数/布尔校验，并如实标注不覆盖权限与数据前置。
"""
import brain
import agenthub_mcp as m


def _call(name, args, root):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": args}}
    return m.handle_message(msg, root)["result"]


def test_protocol_dry_run_rejects_missing_required(hub_root):
    brain.init_db(hub_root)
    res = _call("hub_task_add", {"dry_run": True}, hub_root)
    assert res["isError"] is True, res
    assert "必填参数 title" in res["content"][0]["text"], res
    assert brain.task_count(hub_root) == 0


def test_protocol_dry_run_rejects_bad_int(hub_root):
    brain.init_db(hub_root)
    res = _call("hub_task_add", {"title": "预检", "priority": "abc", "dry_run": True}, hub_root)
    assert res["isError"] is True and "必须是整数" in res["content"][0]["text"], res
    assert brain.task_count(hub_root) == 0


def test_protocol_dry_run_valid_params_preview(hub_root):
    brain.init_db(hub_root)
    res = _call("hub_task_add", {"title": "预检", "dry_run": True}, hub_root)
    assert res["isError"] is False, res
    text = res["content"][0]["text"]
    assert "入参预检通过" in text and "不校验权限" in text, text
    assert brain.task_count(hub_root) == 0


def test_proxy_dry_run_rejects_missing_required(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_call_tool", {
        "name": "hub_task_add", "arguments": {"dry_run": True}}, hub_root)
    assert "必填参数 title" in out and "经 hub_call_tool" in out, out
    assert brain.task_count(hub_root) == 0


def test_proxy_dry_run_valid_params_preview(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_call_tool", {
        "name": "hub_task_add", "arguments": {"title": "预检", "dry_run": True}}, hub_root)
    assert "入参预检通过" in out and "不校验权限" in out, out
    assert brain.task_count(hub_root) == 0
