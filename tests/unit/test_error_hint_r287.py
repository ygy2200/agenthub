# -*- coding: utf-8 -*-
r"""错误提示里的建议必须与工具 schema 同源（R287）。

旧行为：`_error_hint` 按 `tool in WRITE_TOOLS` 建议带 idem_key，但 42 个写工具里
只有 hub_log_work / hub_note / hub_memory_write 三个声明了该参数。模型照提示给
hub_task_add 传 idem_key，会被 R283 的未识别参数提示打回——建议本身不可执行。
"""
import agenthub_mcp as m


def test_transient_hint_idem_only_when_declared():
    h_task = m._error_hint("transient", "hub_task_add")
    assert "可退避重试" in h_task and "idem_key" not in h_task, h_task
    h_mem = m._error_hint("transient", "hub_memory_write")
    assert "idem_key" in h_mem, h_mem
    h_read = m._error_hint("transient", "hub_search")
    assert "idem_key" not in h_read, h_read


def test_ambiguous_hint_idem_only_when_declared():
    h_task = m._error_hint("ambiguous", "hub_task_add")
    assert "idem_key" not in h_task and "只读查询" in h_task, h_task
    h_note = m._error_hint("ambiguous", "hub_note")
    assert "idem_key" in h_note, h_note


def test_err_result_uses_schema_aware_hint():
    text = m._err_result(1, "数据库被锁，请稍后再试", "hub_task_add")["result"]["content"][0]["text"]
    assert "可退避重试" in text and "idem_key" not in text, text
    text2 = m._err_result(1, "数据库被锁，请稍后再试", "hub_log_work")["result"]["content"][0]["text"]
    assert "idem_key" in text2, text2


def test_every_tool_hint_matches_schema():
    """全量扫描：提示提到 idem_key 当且仅当该工具 schema 声明了它。"""
    bad = []
    for t in m.TOOLS:
        name = t["name"]
        declared = "idem_key" in ((t.get("inputSchema") or {}).get("properties") or {})
        for kind in ("transient", "ambiguous"):
            h = m._error_hint(kind, name)
            if ("idem_key" in h) != declared:
                bad.append((name, kind, declared, h))
    assert not bad, bad[:5]


def test_permanent_hint_stays_silent():
    assert m._error_hint("permanent", "hub_task_add") == ""
    assert m._error_hint("permanent", "hub_memory_write") == ""
