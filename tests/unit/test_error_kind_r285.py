# -*- coding: utf-8 -*-
r"""错误分桶的词边界与未知工具归桶（R285）。

旧行为两处失真：
1) transient 关键词 locked 走子串匹配，撞上 hub_tasks 合法状态枚举串里的 blocked——
   纯参数校验错误被标成 transient，提示「可退避重试，等一会儿通常会成功」，
   而重试同一个坏值一万次也是同一个错。
2) 未知工具（工具名拼错）落 ambiguous 兜底，提示「结果不确定：可能已部分生效」——
   实际上什么都没执行。它与参数错同类：改名字再调即可。
"""
import brain
import agenthub_mcp as m


def test_enum_error_is_permanent_not_transient(hub_root):
    brain.init_db(hub_root)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "hub_tasks", "arguments": {"status": "bogus"}}}
    res = m.handle_message(msg, hub_root)["result"]
    assert res["isError"] is True, res
    assert res["structuredContent"]["error_kind"] == "permanent", res
    text = res["content"][0]["text"]
    assert "暂时性失败" not in text and "可退避重试" not in text, text
    assert "参数 status 只能是" in text and "blocked" in text, text


def test_unknown_tool_is_permanent(hub_root):
    brain.init_db(hub_root)
    msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
           "params": {"name": "hub_nope", "arguments": {}}}
    res = m.handle_message(msg, hub_root)["result"]
    assert res["isError"] is True, res
    assert res["structuredContent"]["error_kind"] == "permanent", res
    text = res["content"][0]["text"]
    assert "未知工具" in text, text
    assert "可能已部分生效" not in text, text


def test_blocked_is_not_locked():
    """blocked 里含 locked 子串，但它不是锁冲突。"""
    assert m._error_kind("状态 blocked") == "ambiguous"
    assert m._error_kind("unlocked / deadlocked 也不是锁错误") == "ambiguous"


def test_true_locked_and_timeout_still_transient():
    assert m._error_kind("数据库被锁，请稍后重试") == "transient"
    assert m._error_kind("database is locked (SQLITE_BUSY)") == "transient"
    assert m._error_kind("DB_LOCKED") == "transient"
    assert m._error_kind("request timeout after 30s") == "transient"
    assert m._error_kind("调用超时") == "transient"


def test_permanent_keywords_unchanged():
    assert m._error_kind("错误：记录不存在") == "permanent"
    assert m._error_kind("错误：参数 status 只能是 pending/in_progress/blocked/done") == "permanent"
    assert m._error_kind("错误：未知工具 hub_nope") == "permanent"


def test_no_keyword_falls_back_to_ambiguous():
    assert m._error_kind("") == "ambiguous"
    assert m._error_kind("出现了意料之外的情况") == "ambiguous"


def test_err_result_transient_keeps_hint():
    res = m._err_result(3, "数据库被锁，请稍后再试", "hub_search")["result"]
    assert res["structuredContent"]["error_kind"] == "transient"
    assert "可退避重试" in res["content"][0]["text"]
