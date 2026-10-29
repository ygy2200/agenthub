# -*- coding: utf-8 -*-
r"""枚举入参坏值必须显式拒绝（R282）。

旧行为：schema 声明了 enum，handler 却常不校验——
  hub_tasks(status="bogus")        → 「暂无任务」（像真的没有任务）
  hub_memory_read(kind="bogus")    → 不过滤，返回全量（像过滤生效了）
  hub_view(action="bogus")         → 回落成关注清单（像查询成功）
  hub_note(mood="bogus")           → 落库被静默清空，回执却写「（bogus）」
  hub_events(kind="bogus")         → 参数根本没人读
现在按 schema 集中校验：坏值显式拒绝，大小写/空白归一到规范值；枚举参数全工具扫描兜底。
"""
import brain
import agenthub_mcp as m


def _dummy(prop):
    if prop.get("enum"):
        return prop["enum"][0]
    return {"integer": 1, "boolean": True, "array": [], "object": {}}.get(
        prop.get("type"), "x")


def _enum_params():
    for tool in m.TOOLS:
        props = tool["inputSchema"].get("properties") or {}
        for key, prop in props.items():
            if (isinstance(prop, dict) and isinstance(prop.get("enum"), list)
                    and (tool["name"], key) not in m._ENUM_EXEMPT_PARAMS):
                yield tool, key, prop


def test_every_declared_enum_rejects_bad_value(hub_root):
    """全工具扫描：每个枚举参数传坏值都必须得到「只能是」的清晰错误，而不是空结果。"""
    brain.init_db(hub_root)
    checked = 0
    for tool, key, _prop in _enum_params():
        schema = tool["inputSchema"]
        props = schema.get("properties") or {}
        args = {r: _dummy(props.get(r) or {}) for r in (schema.get("required") or [])}
        args[key] = "__nope__"
        n = args.get("name")
        out = str(m.call_tool(tool["name"], args, hub_root))
        assert f"参数 {key} 只能是" in out, (tool["name"], key, out[:200])
        checked += 1
    assert checked >= 30, checked


def test_declared_enum_values_pass_guard(hub_root):
    """合法值不能被守卫误杀（只断言不是枚举错误）。"""
    brain.init_db(hub_root)
    for tool, key, prop in _enum_params():
        schema = tool["inputSchema"]
        props = schema.get("properties") or {}
        args = {r: _dummy(props.get(r) or {}) for r in (schema.get("required") or [])}
        args[key] = prop["enum"][0]
        out = str(m.call_tool(tool["name"], args, hub_root))
        assert f"参数 {key} 只能是" not in out, (tool["name"], key, out[:200])


def test_tasks_bad_status_is_not_empty_board(hub_root):
    brain.init_db(hub_root)
    brain.task_add(hub_root, "真实任务")
    out = m.call_tool("hub_tasks", {"status": "bogus"}, hub_root)
    assert "错误：参数 status 只能是" in out, out
    assert "暂无任务" not in out, out
    # 合法值（含大写）仍能过滤：归一到规范值后应看到已存在任务
    assert "真实任务" in m.call_tool("hub_tasks", {"status": "PENDING"}, hub_root)


def test_tasks_status_case_insensitive_filters_done(hub_root):
    brain.init_db(hub_root)
    brain.task_add(hub_root, "已完成任务")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE title='已完成任务'")
    out = m.call_tool("hub_tasks", {"status": "DONE"}, hub_root)
    assert "已完成任务" in out, out


def test_memory_read_bad_kind_rejected_and_valid_filters(hub_root):
    brain.init_db(hub_root)
    brain.add_memory(hub_root, "踩坑记忆", kind="lesson")
    brain.add_memory(hub_root, "环境事实", kind="fact")
    bad = m.call_tool("hub_memory_read", {"kind": "bogus"}, hub_root)
    assert "错误：参数 kind 只能是" in bad, bad
    out = m.call_tool("hub_memory_read", {"kind": "LESSON"}, hub_root)
    assert "踩坑记忆" in out and "环境事实" not in out, out


def test_view_bad_action_rejected_without_side_effect(hub_root):
    brain.init_db(hub_root)
    bad = m.call_tool("hub_view", {"action": "bogus", "scope_value": "P", "agent": "a"}, hub_root)
    assert "错误：参数 action 只能是" in bad, bad
    assert brain.view_list(hub_root, "a") == []
    # 大写归一到 follow：真关注（此前会静默回落成 list）
    out = m.call_tool("hub_view", {"action": "FOLLOW", "scope_type": "project",
                                   "scope_value": "P", "agent": "a"}, hub_root)
    assert "已关注" in out, out
    assert [v["scope_value"] for v in brain.view_list(hub_root, "a")] == ["P"]


def test_note_bad_mood_rejected_and_not_written(hub_root):
    brain.init_db(hub_root)
    bad = m.call_tool("hub_note", {"content": "想法", "mood": "bogus"}, hub_root)
    assert "错误：参数 mood 只能是" in bad, bad
    assert brain.notes_count(hub_root) == 0
    ok = m.call_tool("hub_note", {"content": "想法", "mood": "兴奋"}, hub_root)
    assert "（兴奋）" in ok, ok
    with brain.db_conn(hub_root) as conn:
        assert conn.execute("SELECT mood FROM notes").fetchone()[0] == "兴奋"


def test_events_bad_kind_rejected_and_filter_works(hub_root):
    brain.init_db(hub_root)
    brain.event_add(hub_root, "出错事件", kind="error")
    brain.event_add(hub_root, "普通进展", kind="progress")
    bad = m.call_tool("hub_events", {"kind": "bogus"}, hub_root)
    assert "错误：参数 kind 只能是 progress/note/error/handoff" in bad, bad
    out = m.call_tool("hub_events", {"kind": "ERROR"}, hub_root)
    assert "出错事件" in out and "普通进展" not in out, out
    empty = m.call_tool("hub_events", {"kind": "handoff"}, hub_root)
    assert "暂无 handoff 类型事件" in empty and "progress/note/error/handoff" in empty, empty


def test_protocol_dry_run_rejects_bad_enum(hub_root):
    brain.init_db(hub_root)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "hub_tasks", "arguments": {"status": "bogus", "dry_run": True}}}
    res = m.handle_message(msg, hub_root)["result"]
    assert res["isError"] is True, res
    assert "参数 status 只能是" in res["content"][0]["text"], res
