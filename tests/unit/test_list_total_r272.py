# -*- coding: utf-8 -*-
r"""列表口径 R272：信号收件箱 / 记忆检索 / 全盘文件 / 问大脑出处。

同型缺陷族（R260→R265→R267→R271）换位置：`signal_inbox` 固定 LIMIT 30 却把
len(rows) 写成「信号 N 条」，且 schema 早声明了 limit、handler 一直不读；
`hub_memory_read` 取满 limit 时把「取到的」写成「命中的」；`hub_search_files`
取满上限仍报总个数；`hub_ask` 出处静默切 8 条。另守一条更严重的：
未读过滤原先在 SQL LIMIT **之后**做，最新 30 条全读过时 unread_only 返回空。
"""
import agenthub_mcp as m
import brain


def _signals(root, n, to="qa"):
    brain.init_db(root)
    ids = []
    for i in range(n):
        _, sid = brain.signal_send(root, "zcode", f"第{i}条信号", to_agent=to)
        ids.append(sid)
    return ids


def test_signals_over_limit_reports_total(hub_root):
    _signals(hub_root, 35)
    out = m.call_tool("hub_signals", {"agent": "qa"}, hub_root)
    assert "信号 共 35 条，这里列 30 条" in out, out[:300]
    assert "更早的调大 limit" in out


def test_signals_limit_honored(hub_root):
    _signals(hub_root, 12)
    out = m.call_tool("hub_signals", {"agent": "qa", "limit": 5}, hub_root)
    assert "信号 共 12 条，这里列 5 条" in out, out[:300]
    out_all = m.call_tool("hub_signals", {"agent": "qa", "limit": 12}, hub_root)
    assert "这里列" not in out_all, out_all[:300]


def test_unread_filter_applies_before_limit(hub_root):
    """最新 30 条都已读、更早 5 条未读——先取 30 再筛未读的旧实现会返回「无未读」。"""
    ids = _signals(hub_root, 35)
    for sid in ids[5:]:
        brain.signal_mark_read(hub_root, sid, "qa")
    out = m.call_tool("hub_signals", {"agent": "qa", "unread_only": True}, hub_root)
    assert "无未读信号" not in out, out[:300]
    assert "信号 5 条" in out, out[:300]


def test_signal_content_ellipsis(hub_root):
    brain.init_db(hub_root)
    brain.signal_send(hub_root, "zcode", "长" * 120, to_agent="qa")
    out = m.call_tool("hub_signals", {"agent": "qa"}, hub_root)
    assert "…" in out, out[:300]
    assert ("长" * 81) not in out


def test_memory_read_saturation(hub_root):
    brain.init_db(hub_root)
    for i in range(25):
        brain.add_memory(hub_root, f"备份主题{i}：{chr(0x4E00 + i) * 12}", kind="lesson")
    out = m.call_tool("hub_memory_read", {"query": "备份", "limit": 20}, hub_root)
    assert "命中 ≥20 条" in out, out[:300]
    assert "已取满 limit=20" in out, out[:300]
    out2 = m.call_tool("hub_memory_read", {"query": "备份", "limit": 50}, hub_root)
    assert "命中 25 条" in out2, out2[:300]
    assert "已取满" not in out2


def test_search_files_saturation(hub_root, monkeypatch):
    monkeypatch.setattr(
        m.core, "everything_search",
        lambda q, lim: {"results": [f"C:/tmp/x{i}.log" for i in range(min(30, lim))]})
    out = m.call_tool("hub_search_files", {"query": "*.log"}, hub_root)
    assert "≥30 个" in out and "已达 limit=30" in out, out[:300]
    out2 = m.call_tool("hub_search_files", {"query": "*.log", "limit": 50}, hub_root)
    assert "全盘文件 30 个" in out2, out2[:300]


def test_ask_citations_truncation_noted(hub_root, monkeypatch):
    brain.init_db(hub_root)
    cites = [{"type": "记录", "id": i, "kind": "", "date": "2026-10-01"} for i in range(1, 11)]
    monkeypatch.setattr(m.brain, "ask", lambda *a, **k: {
        "intent": "查事实", "confidence": "高", "abstained": False,
        "answer": "答案", "citations": cites, "note": "零 LLM 规则检索"})
    out = m.call_tool("hub_ask", {"question": "备份"}, hub_root)
    assert "另有 2 条出处未列" in out, out[:400]
