# -*- coding: utf-8 -*-
r"""未找到/不可用时要有下一跳（R279）。

零结果的四种死胡同：记录 id 找不到、任务 id 找不到、记忆 id 找不到、Everything 没运行。
只回一句「未找到 #999」等于把排查成本全丢给调用方；这里钉住每条都给出准确入口。
"""
import brain
import core
import agenthub_mcp as m


def test_get_record_not_found_has_next_steps(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_get_record", {"record_id": 999}, hub_root)
    assert "未找到记录 #999" in out, out
    assert "records#" in out and "hub_search" in out and "hub_list_projects" in out, out


def test_quality_missing_task_has_next_steps(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_task_quality", {"task_id": 999}, hub_root)
    assert "不存在" in out and "hub_tasks" in out, out


def test_memory_supersede_missing_has_next_steps(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_memory_supersede", {"keep_id": 1, "drop_id": 2}, hub_root)
    assert "不存在" in out and "hub_memory_read" in out, out


def test_search_files_everything_down_has_fallback(hub_root, monkeypatch):
    brain.init_db(hub_root)
    monkeypatch.setattr(core, "everything_search", lambda q, lim: {"error": "not running"})
    out = m.call_tool("hub_search_files", {"query": "*.py"}, hub_root)
    assert "Everything 搜索不可用" in out and "hub_search" in out, out


def test_search_files_no_match_has_next_steps(hub_root, monkeypatch):
    brain.init_db(hub_root)
    monkeypatch.setattr(core, "everything_search", lambda q, lim: {"results": []})
    out = m.call_tool("hub_search_files", {"query": "*.nope"}, hub_root)
    assert "全盘无匹配文件" in out and "hub_search" in out, out
