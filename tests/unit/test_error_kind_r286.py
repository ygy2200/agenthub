# -*- coding: utf-8 -*-
r"""错误分桶只认「原因」不认「补救词」，执行前闸门不再装成 ambiguous（R286）。

旧行为两处失真：
1) 「重试」「稍后」被当成 transient 关键词——幂等键冲突的文案写着「重试请携原内容」，
   于是被判 transient，提示「等一会儿通常会成功」；而它换内容/换键才是唯一出路。
2) 六类执行前闸门（身份未锁 / 非 manager / 全局冻结 / 确认闸 / 项目绑定不符 /
   幂等键复用）全落 ambiguous，被追加「结果不确定：可能已部分生效」——它们都在
   工具执行前返回，什么都没发生；文案自己已给出下一步。
"""
import brain
import agenthub_mcp as m


def _call(root, name, args):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": args}}
    return m.handle_message(msg, root)["result"]


def test_identity_gates_are_permanent(hub_root):
    brain.init_db(hub_root)
    texts = [
        m._memory_deny(hub_root, "记忆x", "qoder", "supersede", "跨 agent 改写", unverified=True),
        m._manager_deny(hub_root, "hub_freeze", "freeze", "qoder", unverified=True),
        m._manager_deny(hub_root, "hub_freeze", "freeze", "qoder"),
    ]
    for t in texts:
        assert m._error_kind(t) == "permanent", t


def test_freeze_gate_is_permanent(hub_root):
    brain.init_db(hub_root)
    brain.freeze_set(hub_root, 30, "演练", "tester")
    res = _call(hub_root, "hub_task_add", {"title": "冻结中"})
    assert res["isError"] is True, res
    assert res["structuredContent"]["error_kind"] == "permanent", res
    text = res["content"][0]["text"]
    assert "全局冻结" in text and "可退避重试" not in text, text


def test_idem_conflict_is_permanent_not_transient(hub_root):
    brain.init_db(hub_root)
    ok = _call(hub_root, "hub_memory_write", {"content": "第一条记忆", "idem_key": "k1"})
    assert ok["isError"] is False, ok
    bad = _call(hub_root, "hub_memory_write", {"content": "换了内容", "idem_key": "k1"})
    assert bad["isError"] is True, bad
    assert bad["structuredContent"]["error_kind"] == "permanent", bad
    text = bad["content"][0]["text"]
    assert "防键复用" in text, text
    assert "可退避重试" not in text and "可能已部分生效" not in text, text


def test_idem_pending_stays_transient():
    text = ("同一幂等键 'k' 的写入正在处理中，本次未重复执行——"
            "稍后用同一键重试可拿到首次结果（不要换键）")
    assert m._error_kind(text) == "transient", text


def test_retry_word_alone_is_not_a_cause():
    """补救词「重试」不是暂时性证据：没有锁/忙/超时等原因就落 ambiguous。"""
    assert m._error_kind("错误：请重试") == "ambiguous"
    assert m._error_kind("错误：稍后再试") == "ambiguous"
    assert m._error_kind("错误：暂时不可用，请稍后重试") == "transient"
    assert m._error_kind("错误：工具调用触发速率限制，请停止重试") == "transient"


def test_project_mismatch_is_permanent():
    text = ("错误：任务 #1 属于项目「A」，与本次 project「B」不一致——"
            "绑定会把记录记进错误的项目闭环。请把 project 改为「A」，或省略 task 不绑定。")
    assert m._error_kind(text) == "permanent", text


def test_proxy_confirm_gate_is_permanent(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_call_tool",
                      {"name": "hub_task_add",
                       "arguments": {"title": "代理写", "confirm": False}}, hub_root)
    assert "调用未执行" in out and "确认后重发" in out, out
    assert m._error_kind(out) == "permanent", out
