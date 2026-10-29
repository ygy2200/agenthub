# -*- coding: utf-8 -*-
r"""空脑体检的诚实口径（R294）。

实测空脑 `hub_health` 报「结晶率 200.0%」（2 条模板记忆 / 0 条记录，分母被 max(1,0) 兜底），
对外呈现的是假数字；且空脑没有任何「下一步」指引。现在：0 记录时结晶率返回 None
（各展示面显示「—」），hub_health 追加空脑引导。
"""
import brain
import agenthub_mcp as m


def test_empty_brain_crystallization_is_unknown(hub_root):
    brain.init_db(hub_root)
    h = brain.health_report(hub_root, include_dups=False)
    assert h["records"] == 0, h
    assert h["crystallization"] is None, h


def test_empty_brain_health_output_honest(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_health", {}, hub_root)
    assert "结晶率 —（记忆/记录；尚无记录，样本不足）" in out, out
    assert "200.0%" not in out, out
    assert "空脑引导" in out and "hub_create_project" in out and "hub_log_work" in out, out


def test_nonempty_brain_crystallization_numeric(hub_root):
    brain.init_db(hub_root)
    brain.add_record(hub_root, project="P", agent="a", date="",
                     title="t", content="目的：x\n做了什么：y")
    h = brain.health_report(hub_root, include_dups=False)
    assert h["records"] >= 1
    assert isinstance(h["crystallization"], float) and h["crystallization"] >= 0, h
    out = m.call_tool("hub_health", {}, hub_root)
    assert "空脑引导" not in out, out
