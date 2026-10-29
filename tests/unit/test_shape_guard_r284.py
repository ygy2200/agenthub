# -*- coding: utf-8 -*-
r"""参数结构与 schema 类型不符必须显式拒绝（R284）。

旧行为：string 收数组/对象被 str() 成 repr 落库（title=["x"] 建成 "['x']"、
content={"k":1} 存成字典 repr）；object 收字符串被静默换成 {}（action_args="{}" 建出空触发器）；
array 收字符串、number 收坏值一律回落默认。现在按 schema 校验结构，标量入字符串仍宽松。
"""
import brain
import agenthub_mcp as m


def _c(name, args, root):
    return str(m.call_tool(name, args, root))


def test_string_param_rejects_list(hub_root):
    brain.init_db(hub_root)
    out = _c("hub_task_add", {"title": ["数组标题"]}, hub_root)
    assert out == "错误：参数 title 必须是字符串（收到数组）", out
    assert brain.task_count(hub_root) == 0


def test_string_param_rejects_dict(hub_root):
    brain.init_db(hub_root)
    for name, args in (("hub_memory_write", {"content": {"k": 1}}),
                       ("hub_search", {"keyword": {"k": 1}})):
        out = _c(name, args, hub_root)
        assert "必须是字符串（收到对象）" in out, (name, out)
    assert brain.count_memories(hub_root) == 0


def test_object_param_rejects_json_string(hub_root):
    brain.init_db(hub_root)
    out = _c("hub_trigger", {"action": "add", "name": "t", "on_event": "heartbeat",
                            "action_type": "notify", "action_args": '{"text":"hi"}'}, hub_root)
    assert "action_args 必须是对象" in out and "收到字符串" in out, out
    with brain.db_conn(hub_root) as conn:
        assert conn.execute("SELECT COUNT(*) FROM triggers").fetchone()[0] == 0


def test_array_param_rejects_string(hub_root):
    brain.init_db(hub_root)
    out = _c("hub_consolidate", {"ids": "1,2"}, hub_root)
    assert "ids 必须是数组" in out and "收到字符串" in out, out
    out = _c("hub_playbook", {"action": "create", "name": "p", "steps": {"a": 1}}, hub_root)
    assert "steps 必须是数组" in out and "收到对象" in out, out


def test_memory_confirm_comma_ids_still_accepted(hub_root):
    """ids 逗号串是有意容错（handler 自己拆并校验），形状守卫不抢话。"""
    brain.init_db(hub_root)
    out = _c("hub_memory_confirm", {"ids": "1,2"}, hub_root)
    assert "必须是数组" not in out and "跳过 #1" in out, out


def test_number_param_rejects_bad_value(hub_root):
    brain.init_db(hub_root)
    out = _c("hub_memory_write", {"content": "坏置信度", "confidence": "abc"}, hub_root)
    assert "confidence 必须是数字" in out, out
    assert brain.count_memories(hub_root) == 0
    ok = _c("hub_memory_write", {"content": "好置信度", "confidence": "0.8"}, hub_root)
    assert "已写入大脑记忆" in ok, ok
    with brain.db_conn(hub_root) as conn:
        assert conn.execute("SELECT confidence FROM memories").fetchone()[0] == 0.8


def test_scalar_into_string_stays_lenient(hub_root):
    brain.init_db(hub_root)
    out = _c("hub_task_add", {"title": 123, "project": 456}, hub_root)
    assert "任务已建" in out and "123" in out, out
    with brain.db_conn(hub_root) as conn:
        row = conn.execute("SELECT title,project FROM tasks").fetchone()
        assert (row[0], row[1]) == ("123", "456"), row


def test_proxy_arguments_keeps_specific_message(hub_root):
    """hub_call_tool.arguments 由 handler 给同义文案，守卫不抢话。"""
    brain.init_db(hub_root)
    out = _c("hub_call_tool", {"name": "hub_status", "arguments": "{}"}, hub_root)
    assert out.startswith("错误：arguments 需为对象"), out


def test_dry_run_preflight_rejects_shape(hub_root):
    brain.init_db(hub_root)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "hub_task_add",
                      "arguments": {"title": ["x"], "dry_run": True}}}
    res = m.handle_message(msg, hub_root)["result"]
    assert res["isError"] is True, res
    assert "title 必须是字符串" in res["content"][0]["text"], res
    assert brain.task_count(hub_root) == 0
