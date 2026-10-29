# -*- coding: utf-8 -*-
r"""首次使用的空态要能自己走下一步（R278）。

空态只描述「没有数据」不够：新用户（或新接入的 agent）下一步该调什么是唯一有用信息。
本组用例钉住五个空态的「怎么产生第一条」提示，同时确认有数据后提示不再出现。
"""
import brain
import agenthub_mcp as m


def test_projects_empty_teaches_create(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_list_projects", {}, hub_root)
    assert "共 0 个项目" in out, out
    assert "hub_create_project" in out and "hub_heartbeat" in out, out


def test_projects_nonempty_has_no_empty_hint(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_create_project", {"project": "甲-空态测试", "agent": "qa"}, hub_root)
    assert "错误" not in out, out
    out = m.call_tool("hub_list_projects", {}, hub_root)
    assert "还没有项目" not in out, out
    assert "甲-空态测试" in out, out


def test_agents_empty_teaches_register(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_list_agents", {}, hub_root)
    assert "注册表为空" in out, out
    assert "hub_heartbeat" in out and "hub_agent(action=\"verify\")" in out, out


def test_progress_empty_teaches_first_log(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_get_progress", {}, hub_root)
    assert "还没有工作记录" in out, out
    assert "hub_heartbeat" in out and "hub_log_work" in out, out


def test_todos_empty_teaches_phrasing(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_list_todos", {}, hub_root)
    assert "未发现待办" in out and "下一步" in out and "待办：" in out, out


def test_quality_empty_teaches_window_and_done(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_task_quality", {}, hub_root)
    assert "窗口内没有已完成任务" in out, out
    assert "days" in out and "hub_task_update(status=\"done\")" in out, out
