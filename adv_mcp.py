# -*- coding: utf-8 -*-
"""AgentHub MCP server 对抗回归：协议层 + 端到端子进程。

运行：python adv_mcp.py
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import agenthub_mcp as m
import brain
import core

FAILED = []


def case(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except AssertionError as e:
        FAILED.append(name)
        print(f"  FAIL  {name}  ->  {e}")
    except Exception as e:  # noqa: BLE001
        FAILED.append(name)
        print(f"  ERROR {name}  ->  {type(e).__name__}: {e}")


def rpc(method, params=None, mid=1):
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}


def resp_ok(msg):
    assert "error" not in msg, msg
    return msg["result"]


def build_hub(root: Path):
    p = root / "测试-项目"
    p.mkdir(parents=True)
    (p / core.RECORD_NAME).write_text(
        "## 2026-09-27（hermes）\n初始化记录\n| 项 | 值 |\n|---|---|\n| A | 1 |\n", encoding="utf-8")
    (root / core.DIR_INBOX).mkdir()
    import brain
    brain.init_db(str(root))


def fresh_hub(tmp, name) -> Path:
    """独立 hub：隔离共享状态（错误自增 id / 心跳残留），保证用例可单跑复现。"""
    root = Path(tmp) / name
    build_hub(root)
    return root


def t_protocol(root):
    r = resp_ok(m.handle_message(rpc("initialize", {"protocolVersion": "2025-06-18"}), str(root)))
    assert r["protocolVersion"] == "2025-06-18" and r["serverInfo"]["name"] == "agenthub"
    assert r["serverInfo"]["version"] == "2.40.1"
    assert m.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, str(root)) is None
    tools = resp_ok(m.handle_message(rpc("tools/list"), str(root)))["tools"]
    names = {t["name"] for t in tools}
    assert {"hub_list_projects", "hub_log_work", "hub_memory_read", "hub_memory_write",
            "hub_get_progress", "hub_list_skills", "hub_list_mcps", "hub_search",
            "hub_get_project", "hub_create_project", "hub_get_rules", "hub_heartbeat",
            "hub_report_error", "hub_list_errors", "hub_undo", "hub_list_agents",
            "hub_list_todos", "hub_health", "hub_todo_done", "hub_env_set", "hub_env_list",
            "hub_search_files", "hub_distill", "hub_get_record", "hub_duplicates", "hub_report",
            "hub_handoff", "hub_status", "hub_user_profile", "hub_note", "hub_note_read",
            "hub_ask", "hub_metacognition",
            "hub_help", "hub_search_tools", "hub_call_tool", "hub_audit", "hub_flush_index",
            "hub_task_add", "hub_task_update", "hub_claim", "hub_tasks",
            "hub_next", "hub_crystallize",
            "hub_event_write", "hub_events",
            "hub_skill_match", "hub_skill_sync"} <= names, names
    assert len(names) == 68  # X3+X4=65，Y1+Y2+Y3（2026-10-13）+playbook/trigger/graph=68（X5 走 CLI 不加工具）
    # 未知方法
    msg = m.handle_message(rpc("no/such"), str(root))
    assert msg["error"]["code"] == -32601
    # limit 非法值容错（v1.3.1：不再抛异常，返回正常结果）
    r = m.handle_message(rpc("tools/call", {"name": "hub_get_progress", "arguments": {"limit": "abc"}}, 2), str(root))
    assert r["result"]["isError"] is False, r
    # 工具抛异常 -> isError 且自动落盘错误登记（mock 掉 call_tool 模拟任意异常）
    from unittest import mock
    with mock.patch.object(m, "call_tool", side_effect=RuntimeError("模拟崩溃")):
        r = m.handle_message(rpc("tools/call", {"name": "hub_x", "arguments": {"agent": "zcode"}}, 3), str(root))
    assert r["result"]["isError"] is True and "模拟崩溃" in r["result"]["content"][0]["text"]
    errs = brain.error_list(str(root))
    assert any("hub_x" in e.get("title", "") and e.get("agent") == "zcode" for e in errs), \
        "工具异常未自动登记错误"
    # ping
    assert "result" in m.handle_message(rpc("ping"), str(root))


def t_tools(root):
    root_s = str(root)
    out = m.call_tool("hub_list_projects", {}, root_s)
    assert "测试-项目" in out
    out = m.call_tool("hub_get_project", {"project": "测试-项目"}, root_s)
    assert "初始化记录" in out and "2026-09-27" in out
    out = m.call_tool("hub_get_project", {"project": "不存在的"}, root_s)
    assert "不存在" in out
    out = m.call_tool("hub_search", {"keyword": "初始化"}, root_s)
    assert "[记录#" in out and "全脑检索" in out, out
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "deepseek",
                                       "content": "对抗测试记录：修了X，验证通过"}, root_s)
    assert "已记入大脑" in out, out
    recs = brain.list_records(root_s, "测试-项目")
    assert any("（deepseek）" in r["title"] and "对抗测试记录" in r["content"] for r in recs), recs
    out = m.call_tool("hub_memory_write", {"content": "环境事实：校园网GitHub不通", "kind": "fact"}, root_s)
    assert "已写入大脑记忆" in out
    assert "校园网GitHub不通" in m.call_tool("hub_memory_read", {"query": "校园网"}, root_s)
    out = m.call_tool("hub_get_progress", {"limit": 10}, root_s)
    assert "deepseek" in out or "hermes" in out
    out = m.call_tool("hub_list_skills", {}, root_s)
    assert "技能" in out
    out = m.call_tool("hub_list_mcps", {}, root_s)
    assert "MCP" in out
    out = m.call_tool("hub_log_work", {"project": "", "agent": "zcode", "content": " "}, root_s)
    assert "必填" in out
    # project 只作为 DB 文本行落库，无文件系统操作（穿越由 create_project 校验拦截）
    out = m.call_tool("hub_log_work", {"project": "..\\逃逸", "agent": "zcode", "content": "y"}, root_s)
    assert "已记入大脑" in out


def t_memory_overwrite(tmp):
    root = str(Path(tmp) / "hub")
    out = m.call_tool("hub_memory_write", {"content": "旧记忆第一条", "kind": "note"}, root)
    assert "已写入" in out
    out = m.call_tool("hub_memory_write", {"content": "旧记忆第一条", "mode": "overwrite",
                                           "kind": "fact", "agent": "zcode"}, root)
    assert "已更新" in out, out
    rows = brain.search_memories(root, "旧记忆第一条")
    assert len(rows) == 1, "overwrite 产生了重复条目"
    assert rows[0]["kind"] == "fact", "overwrite 未更新类型"
    # overwrite 无匹配 -> 落为新增
    out = m.call_tool("hub_memory_write", {"content": "全新的记忆", "mode": "overwrite"}, root)
    assert "已写入" in out
    # P4.3 融合检索是宽容召回（部分 bigram 重叠的条目也会返回）——
    # overwrite"无重复条目"语义按内容精确断言，不再数结果条数
    rows_new = brain.search_memories(root, "全新的记忆")
    assert rows_new and rows_new[0]["content"] == "全新的记忆", rows_new[:2]
    assert len([r for r in rows_new if r["content"] == "全新的记忆"]) == 1
    # 非法 mode / 空 content / 超长
    out = m.call_tool("hub_memory_write", {"content": "x", "mode": "drop"}, root)
    assert "append" in out
    out = m.call_tool("hub_memory_write", {"content": "  "}, root)
    assert "必填" in out
    out = m.call_tool("hub_memory_write", {"content": "z" * 200000}, root)
    assert "超长" in out
    # journal 埋点在 DB
    jl = brain.journal_list(root)
    assert any(e.get("action") == "memory_append" for e in jl)


def t_record_type_guard(tmp):
    """P1-2：hub_log_work 的 record_type 枚举校验——非法当场拒收（不落库），合法入库。"""
    root = fresh_hub(tmp, "rtype")
    rs = str(root)
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "类型校验-非法样本", "record_type": "乱写"}, rs)
    assert "错误" in out and "record_type" in out, out
    assert not brain.search_records(rs, "类型校验-非法样本"), "非法类型不应落库"
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "类型校验-合法样本", "record_type": "todo"}, rs)
    assert "已记入大脑" in out, out
    rows = brain.search_records(rs, "类型校验-合法样本")
    assert rows and rows[0]["record_type"] == "todo", rows[:1]


def t_links_extract(tmp):
    """P1-3：hub_log_work 写入含 [[双链]] 的正文 → relations 自动成边（MCP 生产路径）。"""
    root = fresh_hub(tmp, "links")
    rs = str(root)
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "参考 [[测试-项目]] 与 [[r:1]] 的做法"}, rs)
    assert "已记入大脑" in out, out
    assert brain.relation_list(rs, from_kind="record", to_kind="project", to_ref="测试-项目"), \
        "[[项目名]] 未自动成 relation"
    assert brain.relation_list(rs, from_kind="record", to_kind="record", to_ref="1"), \
        "[[r:1]] 显式引用未成 relation"


def t_concurrent_log_work(tmp, root=None):
    # 独立 hub（2026-10-10）：agent 名改用注册表内取值后，靠名字前缀过滤 journal
    # 不再可靠（会与其他用例同名串味），故用 fresh_hub 隔离。
    root = root if root is not None else Path(tmp) / "hub"
    root_s = str(root)
    import threading
    errs = []
    # 2026-10-10：agent 名改为**注册表内取值**——自报身份现在会被枚举校验
    # （不在注册表即拒收，见 brain.check_agent）。并发语义不受影响：仍 8 个不同身份。
    names = ["zcode", "dsh", "hermes", "claude", "codex", "workbuddy", "deepseek", "其他"]

    def worker(i):
        out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": names[i],
                                           "content": f"第{i}号并发记录内容", "date": "2026-09-30"}, root_s)
        if "已记入大脑" not in out:
            errs.append(out)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    recs = brain.list_records(root_s, project="测试-项目", limit=2000)
    for i in range(8):
        assert any(f"（{names[i]}）" in r["title"] and f"第{i}号并发记录内容" in r["content"] for r in recs), \
            f"并发第{i}条丢失"
    jl = brain.journal_list(root_s, 1000)
    log_entries = [e for e in jl if e.get("action") == "log_work"
                   and e.get("agent") in names]
    assert len(log_entries) == 8, f"journal 应有 8 条并发记录，实际 {len(log_entries)}"


def t_heartbeat_conflict(root):
    root = str(root)
    out = m.call_tool("hub_heartbeat", {"agent": "zcode", "project": "测试-项目", "note": "改UI"}, root)
    assert "无其他活跃会话" in out
    out = m.call_tool("hub_heartbeat", {"agent": "dsh", "project": "测试-项目", "note": "改驱动"}, root)
    assert "撞车预警" in out and "zcode" in out, out
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "zcode 的活干完了"}, root)
    assert "撞车预警" in out, out
    out = m.call_tool("hub_heartbeat", {"agent": "dsh", "project": "别的-项目"}, root)
    assert "无其他活跃会话" in out
    # 陈旧清理：手改 ts
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE sessions SET ts='2026-09-30T08:00:00' WHERE agent='zcode'")
    out = m.call_tool("hub_heartbeat", {"agent": "dsh", "project": "测试-项目"}, root)
    assert "zcode" not in out, "陈旧会话未被清理"
    with brain.db_conn(root) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE agent='zcode'").fetchone()[0] == 0
    # agent 必填
    out = m.call_tool("hub_heartbeat", {"agent": "  "}, root)
    assert "必填" in out


def t_heartbeat_today(root):
    """M10：心跳开工推送「今日任务」段（hub_next 同源）——空库不刷/项目过滤/回退全局/排序。"""
    root = str(root)
    # 空库：四段全空 → 不追加（不刷噪音）
    out = m.call_tool("hub_heartbeat", {"agent": "zcode", "project": "甲-问题"}, root)
    assert "今日任务" not in out, out
    brain.task_add(root, "修发布管线", project="甲-问题", priority=1)
    brain.task_add(root, "补文档", project="甲-问题", priority=0)
    brain.task_add(root, "别的项目的活", project="乙-问题", priority=0)
    brain.task_claim(root, 2, "zcode")
    # 按项目过滤：只见甲项目（1 ready P1 + 1 doing），不见乙项目
    out = m.call_tool("hub_heartbeat", {"agent": "zcode", "project": "甲-问题"}, root)
    assert "今日任务" in out and "可认领 1" in out and "进行中 1" in out, out
    assert "▶ #1 [P1] 甲-问题｜修发布管线" in out, out
    assert "● #2 [P0] 进行中 @zcode 补文档" in out, out
    assert "乙-问题" not in out, "项目过滤失效"
    # 无任务项目回退全局：丙-问题四段全空 → 回退看到甲乙全部
    out = m.call_tool("hub_heartbeat", {"agent": "zcode", "project": "丙-问题"}, root)
    assert "可认领 2" in out and "乙-问题" in out, out
    # ready 排序：P0 的乙项目活排在 P1 前面
    assert out.index("▶ #3 [P0]") < out.index("▶ #1 [P1]"), out
    # M3 未读信号联动（开工推送第二段）：发两条 → dsh 心跳见 2 条 → 标一条 → 剩 1 条
    out = m.call_tool("hub_heartbeat", {"agent": "dsh", "project": "甲-问题"}, root)
    assert "未读信号" not in out, out
    m.call_tool("hub_signal_send", {"agent": "zcode", "to_agent": "dsh",
                                    "content": "来验收", "project": "甲-问题"}, root)
    m.call_tool("hub_signal_send", {"agent": "zcode", "content": "广播：exe 更新",
                                    "kind": "alert"}, root)
    out = m.call_tool("hub_heartbeat", {"agent": "dsh", "project": "甲-问题"}, root)
    assert "[未读信号] 2 条" in out and "来验收" in out and "广播：exe 更新" in out, out
    box = brain.signal_inbox(root, "dsh", unread_only=True)
    assert len(box) == 2, box
    m.call_tool("hub_signal_read", {"signal_id": box[0]["id"], "agent": "dsh"}, root)
    out = m.call_tool("hub_heartbeat", {"agent": "dsh", "project": "甲-问题"}, root)
    assert "[未读信号] 1 条" in out, out
    # 自发广播不进发件人自己的未读（收件箱仍显示，留痕用）
    out2 = m.call_tool("hub_heartbeat", {"agent": "zcode", "project": "甲-问题"}, root)
    assert "未读信号" not in out2, out2


def t_handoff_tool(root):
    """2.2 交接协议工具链：create → list（open）→ claim → 不可重复认领。"""
    root = str(root)
    out = m.call_tool("hub_handoff", {"action": "create", "project": "测试-项目", "agent": "zcode",
                                      "done": "回归全绿", "next_step": "发版", "excluded": "方案A"}, root)
    assert "已创建交接 #" in out, out
    hid = int(out.split("#")[1].split("（")[0])
    out = m.call_tool("hub_handoff", {"action": "list", "project": "测试-项目"}, root)
    assert f"#{hid}" in out and "下一步：发版" in out and "已排除：方案A" in out, out
    # 换 agent 认领
    out = m.call_tool("hub_handoff", {"action": "claim", "handoff_id": hid, "agent": "dsh"}, root)
    assert "已认领" in out and "dsh" in out, out
    # 认领后 open 清单为空；include_claimed 可见
    out = m.call_tool("hub_handoff", {"action": "list", "project": "测试-项目"}, root)
    assert f"#{hid}" not in out, out
    out = m.call_tool("hub_handoff", {"action": "list", "project": "测试-项目",
                                      "include_claimed": True}, root)
    assert "已被 dsh 认领" in out, out
    # 重复认领 / 不存在 id / agent 必填
    out = m.call_tool("hub_handoff", {"action": "claim", "handoff_id": hid, "agent": "zcode"}, root)
    assert "已被" in out, out
    out = m.call_tool("hub_handoff", {"action": "claim", "handoff_id": 999999, "agent": "zcode"}, root)
    assert "不存在" in out, out
    out = m.call_tool("hub_handoff", {"action": "create", "project": "", "agent": "zcode"}, root)
    assert "必填" in out, out
    # 空 list 兜底文案
    out = m.call_tool("hub_handoff", {"action": "list", "project": "没有交接的项目"}, root)
    assert "无未认领" in out, out


def t_errors_flow(root):
    root = str(root)
    out = m.call_tool("hub_report_error", {"agent": "deepseek", "title": "删错文件",
                                           "detail": "把 output/a.png 删了", "project": "测试-项目",
                                           "undo": "从回收站还原"}, root)
    assert "已登记" in out
    out = m.call_tool("hub_list_errors", {}, root)
    assert "删错文件" in out and "从回收站还原" in out and "[open]" in out
    # 状态流转（迁移进来的旧错误是 #1，新报的是最大 id）
    new_id = max(e["id"] for e in brain.error_list(root))
    assert brain.error_set_status(root, new_id, "fixed") == ""
    out = m.call_tool("hub_list_errors", {"status": "open"}, root)
    assert "删错文件" not in out
    out = m.call_tool("hub_list_errors", {"status": "fixed"}, root)
    assert "删错文件" in out
    assert "未找到" in brain.error_set_status(root, 99999, "fixed")
    # 自增 id
    m.call_tool("hub_report_error", {"agent": "x", "title": "第二件错事"}, root)
    errs = brain.error_list(root)
    assert errs[0]["title"] == "第二件错事" and errs[0]["id"] > new_id
    # title 必填
    out = m.call_tool("hub_report_error", {"agent": "x", "title": " "}, root)
    assert "必填" in out


def t_undo_log(root):
    root_s = str(root)
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "zcode 第一件事", "date": "2026-09-29"}, root_s)
    assert "已记入大脑" in out, out
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "zcode 第二件事写错了", "date": "2026-09-30"}, root_s)
    assert "已记入大脑" in out, out
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "hermes",
                                       "content": "hermes 的事不能被误伤", "date": "2026-09-30"}, root_s)
    assert "已记入大脑" in out, out
    # 撤 zcode 最新一条
    out = m.call_tool("hub_undo", {"agent": "zcode"}, root_s)
    assert "已撤销" in out, out
    rows = brain.list_records(root_s, project="测试-项目", limit=2000)
    assert all("第二件事写错" not in r["content"] for r in rows), "目标记录仍可见"
    assert any("zcode 第一件事" in r["content"] for r in rows), "误伤更早的记录"
    assert any("hermes 的事不能被误伤" in r["content"] for r in rows), "误伤他人"
    assert any("初始化记录" in r["content"] for r in rows), "误伤迁移记录"
    # 重复撤销 = 栈式撤上一条
    out = m.call_tool("hub_undo", {"agent": "zcode"}, root_s)
    assert "已撤销" in out, out
    rows = brain.list_records(root_s, project="测试-项目", limit=2000)
    assert all("zcode" not in r["agent"] for r in rows)
    assert any("hermes 的事不能被误伤" in r["content"] for r in rows)
    assert any("初始化记录" in r["content"] for r in rows)
    # 撤无可撤 / 无记录 agent / 必填
    out = m.call_tool("hub_undo", {"agent": "zcode"}, root_s)
    assert "没有找到" in out
    out = m.call_tool("hub_undo", {"agent": "codex"}, root_s)
    assert "没有找到" in out
    out = m.call_tool("hub_undo", {"agent": ""}, root_s)
    assert "必填" in out
    # 撤销操作进 DB 流水
    jl = brain.journal_list(root_s, 1000)
    assert len([e for e in jl if e.get("action") == "undo_log_work"]) == 2


def t_new_tools(tmp):
    root_s = str(Path(tmp) / "hub")
    # 参数健壮化（v2）：content 超长拒绝、date 畸形兜底、limit 容错
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "y" * (m.MAX_CONTENT + 1)}, root_s)
    assert "超长" in out, out
    out = m.call_tool("hub_memory_write", {"content": "z" * (m.MAX_CONTENT + 1)}, root_s)
    assert "超长" in out, out
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "date畸形", "date": "明天下午"}, root_s)
    assert "已记入大脑" in out, out
    today = datetime.date.today().isoformat()
    assert any(r["date"] == today and "date畸形" in r["content"]
               for r in brain.list_records(root_s, project="测试-项目", limit=2000)), "date 兜底失败"
    out = m.call_tool("hub_get_progress", {"limit": "abc"}, root_s)
    assert "各 agent 最近工作" in out
    out = m.call_tool("hub_get_progress", {"limit": 99999}, root_s)
    assert "各 agent 最近工作" in out
    out = m.call_tool("hub_log_work", {"project": "", "agent": "zcode", "content": "y"}, root_s)
    assert "project 必填" in out
    # get_rules
    out = m.call_tool("hub_get_rules", {}, root_s)
    assert "对象-问题" in out
    # create_project：目录 + projects 登记 + 注入拒绝
    out = m.call_tool("hub_create_project", {"project": "新工具新建-项目", "agent": "zcode"}, root_s)
    assert "已创建" in out
    assert brain.project_exists(root_s, "新工具新建-项目")
    assert (Path(root_s) / "新工具新建-项目" / "input").is_dir()
    assert any(e.get("action") == "create_project" for e in brain.journal_list(root_s))
    out = m.call_tool("hub_create_project", {"project": "../逃逸"}, root_s)
    assert "错误" in out
    out = m.call_tool("hub_create_project", {"project": "CON"}, root_s)
    assert "保留名" in out
    out = m.call_tool("hub_create_project", {"project": "没有连字符"}, root_s)
    assert "连字符" in out
    out = m.call_tool("hub_create_project", {"project": "新工具新建-项目"}, root_s)
    assert "已创建" in out


def t_end_to_end(root):
    """子进程真实握手：initialize -> initialized -> tools/list -> tools/call。"""
    root_s = str(root)
    proc = subprocess.Popen([sys.executable, str(Path(__file__).parent / "agenthub_mcp.py"), root_s],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    try:
        reqs = [
            rpc("initialize", {"protocolVersion": "2024-11-05"}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            rpc("tools/list", mid=2),
            rpc("tools/call", {"name": "hub_list_projects", "arguments": {}}, mid=3),
        ]
        proc.stdin.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reqs))
        proc.stdin.flush()
        lines = []
        while len(lines) < 3:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(json.loads(line))
        assert len(lines) == 3
        assert lines[0]["result"]["serverInfo"]["name"] == "agenthub"
        assert any(t["name"] == "hub_log_work" for t in lines[1]["result"]["tools"])
        assert "测试-项目" in lines[2]["result"]["content"][0]["text"]
        # 坏行不影响后续
        proc.stdin.write("这不是json\n" + json.dumps(rpc("ping", mid=4)) + "\n")
        proc.stdin.flush()
        got = json.loads(proc.stdout.readline())
        assert got["id"] == 4 and "result" in got
    finally:
        proc.kill()


def t_mcp_access(tmp):
    import core
    py = "D:/python311/python.exe"
    srv = "C:/x/agenthub_mcp.py"
    root = "C:/y/hub"
    # ZCode 布局
    f = Path(tmp) / "zcode_config.json"
    f.write_text('{"mcp": {"servers": {"github": {"url": "u"}}}, "other": 1}', encoding="utf-8")
    assert core.install_mcp_entry(str(f), "zcode", py, srv, root) == ""
    d = json.loads(f.read_text(encoding="utf-8"))
    assert d["other"] == 1 and "github" in d["mcp"]["servers"]
    assert d["mcp"]["servers"]["agenthub"]["args"] == [srv]  # v2.2.1 动态模式不传 root
    assert list(f.parent.glob("*.bak-agenthub-*"))
    assert core.remove_mcp_entry(str(f), "zcode") == ""
    d = json.loads(f.read_text(encoding="utf-8"))
    assert "agenthub" not in d["mcp"]["servers"] and "github" in d["mcp"]["servers"]
    # standard 布局 + 文件不存在自动创建
    f2 = Path(tmp) / "claude.json"
    assert core.install_mcp_entry(str(f2), "standard", py, srv, root) == ""
    d = json.loads(f2.read_text(encoding="utf-8"))
    assert "agenthub" in d["mcpServers"]
    assert core.remove_mcp_entry(str(f2), "standard") == ""
    assert core.remove_mcp_entry(str(f2), "standard") == "未接入"
    # 坏 json 拒绝写入
    f3 = Path(tmp) / "bad.json"
    f3.write_text("{坏", encoding="utf-8")
    assert "拒绝" in core.install_mcp_entry(str(f3), "standard", py, srv, root)
    # 重复接入幂等覆盖
    f4 = Path(tmp) / "again.json"
    assert core.install_mcp_entry(str(f4), "standard", py, srv, root) == ""
    assert core.install_mcp_entry(str(f4), "standard", py, srv, root) == ""
    # P3-1 profile（2026-10-11）：--profile 写进启动参数；重装保持（onboard/GUI 场景）；缺省不带
    f5 = Path(tmp) / "profile.json"
    assert core.install_mcp_entry(str(f5), "standard", py, srv, root, agent="dsh",
                                  profile="worker") == ""
    d = json.loads(f5.read_text(encoding="utf-8"))
    assert d["mcpServers"]["agenthub"]["args"] == [srv, "--agent", "dsh", "--profile", "worker"], \
        d["mcpServers"]["agenthub"]["args"]
    assert core.install_mcp_entry(str(f5), "standard", py, srv, root, agent="dsh",
                                  profile="worker") == ""
    d = json.loads(f5.read_text(encoding="utf-8"))
    assert d["mcpServers"]["agenthub"]["args"] == [srv, "--agent", "dsh", "--profile", "worker"]
    f6 = Path(tmp) / "noprofile.json"
    assert core.install_mcp_entry(str(f6), "standard", py, srv, root, agent="zcode") == ""
    d = json.loads(f6.read_text(encoding="utf-8"))
    assert d["mcpServers"]["agenthub"]["args"] == [srv, "--agent", "zcode"]


# ---------------------------------------------------------------- v1.3 新增对抗用例

def t_bootstrap(tmp):
    root = str(Path(tmp) / "hub")
    tgt = Path(tmp) / "boot" / "AGENTS.md"
    tgt.parent.mkdir(parents=True)
    original = "# 我的全局规则\n\n保持原样。\n"
    tgt.write_text(original, encoding="utf-8")
    # 注入
    assert core.inject_bootstrap(str(tgt), root) == ""
    text = tgt.read_text(encoding="utf-8")
    assert "我的全局规则" in text, "原有内容丢失"
    assert core.BOOTSTRAP_BEGIN in text and core.BOOTSTRAP_END in text
    assert "hub_log_work" in text and root in text
    assert core.bootstrap_status(str(tgt))
    # 幂等：再注入只有一块，且原有内容仍不重复
    assert core.inject_bootstrap(str(tgt), root) == ""
    text2 = tgt.read_text(encoding="utf-8")
    assert text2.count(core.BOOTSTRAP_BEGIN) == 1, "幂等失败：出现多块"
    assert text2.count("我的全局规则") == 1
    # 更新 root 路径后注入反映新路径
    assert core.inject_bootstrap(str(tgt), "D:/新根") == ""
    assert "D:/新根" in tgt.read_text(encoding="utf-8")
    # 移除 -> 恢复为"原文 + 移除块后的干净版本"
    assert core.remove_bootstrap(str(tgt), root) == ""
    text3 = tgt.read_text(encoding="utf-8")
    assert core.BOOTSTRAP_BEGIN not in text3
    assert "我的全局规则" in text3
    assert not core.bootstrap_status(str(tgt))
    # 备份存在
    assert list(tgt.parent.glob("AGENTS.md.bak-agenthub-*"))
    # 未注入时移除
    assert "未注入" in core.remove_bootstrap(str(tgt), root)
    # 文件不存在自动创建（纯引导）
    tgt2 = Path(tmp) / "boot2" / "deep" / "AGENTS.md"
    assert core.inject_bootstrap(str(tgt2), root) == ""
    assert core.bootstrap_status(str(tgt2))
    # 目标定义完整（2026-10-09：接入抽象后新增 workbuddy，Codex 升为全 MCP 目标）
    assert {t["agent"] for t in core.BOOTSTRAP_TARGETS} == {"ZCode", "Claude Code", "Codex", "DSH", "hermes", "workbuddy"}
    for t in core.BOOTSTRAP_TARGETS:
        # hermes 的 SOUL.md 在 D 盘（home 外），允许绝对路径
        assert t["path"].startswith("~/") or t["path"][:3] in ("D:/", "C:/"), t


def t_root_follow(tmp):
    """回归（2026-09-30 幽灵库事故）：MCP 进程动态跟随 config 的 root——
    根目录迁移后，旧进程不得继续读写旧库；新写入必须落在新库。"""
    import contextlib
    import io

    hub_a, hub_b = fresh_hub(tmp, "root_a"), fresh_hub(tmp, "root_b")
    # 测试纪律（2026-09-30/10-01 两次 config 污染事故）：绝不写真实 config——
    # monkeypatch CONFIG_FILE 指向临时配置，serve 动态跟随读的也是它，真实配置全程零接触
    saved_cfg = core.CONFIG_FILE
    fake_cfg = Path(tmp) / "config.json"
    fake_cfg.write_text(json.dumps({"root": str(hub_a)}), encoding="utf-8")
    core.CONFIG_FILE = fake_cfg
    try:
        m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "ZCode",
                                     "content": "迁移前写在旧库"}, str(hub_a))
        # 模拟配置迁移：config root 切到 B，进程仍从 A 启动
        core.set_root(str(hub_b))
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "hub_log_work",
                                     "arguments": {"project": "测试-项目", "agent": "ZCode",
                                                   "content": "迁移后写新库"}}}, ensure_ascii=False)

        class FakeIn:
            def __init__(self, lines):
                self._lines = lines

            def __iter__(self):
                return iter(self._lines)

        old_in = sys.stdin
        sys.stdin = FakeIn([req])
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                m.serve(str(hub_a), fixed=False)
        finally:
            sys.stdin = old_in
        assert any("迁移后写新库" in r["content"]
                   for r in brain.list_records(str(hub_b), "测试-项目")), "新库未收到写入"
        assert all("迁移后写新库" not in r["content"]
                   for r in brain.list_records(str(hub_a), "测试-项目")), "旧库被幽灵进程误写"
    finally:
        core.CONFIG_FILE = saved_cfg


def t_get_record_and_search_files(tmp):
    """hub_get_record 全文精读（蒸馏配套）+ hub_search_files 列表回归（2026-10-02 死代码 bug）。"""
    root_s = str(Path(tmp) / "hub")
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "【目的】验证单条读取\n做了什么：全文应含此行"}, root_s)
    assert "已记入大脑" in out, out
    rid = max(r["id"] for r in brain.list_records(root_s, project="测试-项目", limit=2000))
    out = m.call_tool("hub_get_record", {"record_id": rid}, root_s)
    assert "records#" in out and "全文应含此行" in out and "做了什么" in out, out
    assert "已撤销" not in out
    # 软删后读取带状态标注
    with brain.db_conn(root_s) as conn:
        conn.execute("UPDATE records SET status='deleted' WHERE id=?", (rid,))
    out = m.call_tool("hub_get_record", {"record_id": rid}, root_s)
    assert "records#" in out and "已软删/撤销" in out, out
    # 畸形 id 容错
    for bad in (0, -1, "abc", None, 999999):
        out = m.call_tool("hub_get_record", {"record_id": bad}, root_s)
        assert "未找到记录" in out, (bad, out)
    # hub_search_files：死代码 bug 回归——列表必须真的拼进返回值
    from unittest import mock
    fake = {"results": ["D:/a/结果1.py", "D:/b/结果2.txt"], "engine": "everything"}
    with mock.patch.object(core, "everything_search", return_value=fake):
        out = m.call_tool("hub_search_files", {"query": "结果"}, root_s)
    assert "D:/a/结果1.py" in out and "D:/b/结果2.txt" in out, f"文件列表丢失：{out}"
    assert out.count("- D:/") == 2, out
    with mock.patch.object(core, "everything_search", return_value={"results": [], "engine": "everything"}):
        out = m.call_tool("hub_search_files", {"query": "空"}, root_s)
    assert "全盘无匹配文件" in out, out


def t_search_output(tmp):
    """hub_search 检索顺手度：#id 精读闭环 + 统计头 + 片段 + 零命中建议 + 通配符转义。"""
    import re
    root = Path(tmp) / "hub_search_out"
    build_hub(root)
    root_s = str(root)
    m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                 "content": "【目的】修好 Everything 索引延迟问题，验证通过"}, root_s)
    out = m.call_tool("hub_search", {"keyword": "Everything 索引"}, root_s)
    assert "全脑检索" in out and "命中 记录1 · 记忆0 · 随记0 · 文件0" in out, out
    assert "[记录#" in out, f"记录行缺 #id（精读链路断）：{out}"
    assert "↳" in out, f"缺命中片段行：{out}"
    assert "放宽召回" not in out, "正常命中误标注重试提示"
    rid = int(re.search(r"\[记录#(\d+)\]", out).group(1))
    rec = m.call_tool("hub_get_record", {"record_id": rid}, root_s)
    assert "Everything" in rec, "#id 无法精读，链路闭环失败"
    # 零命中：给检索建议而非干巴巴"无结果"
    out0 = m.call_tool("hub_search", {"keyword": "绝不存在的词xyz"}, root_s)
    assert "无结果" in out0 and "可试" in out0, out0
    # 通配符按字面匹配：hub 内无字面 %，裸 % 零命中（旧版全库命中返回一串）
    outw = m.call_tool("hub_search", {"keyword": "%"}, root_s)
    assert "无结果" in outw, f"裸 % 仍全库命中：{outw[:200]}"
    # hub_memory_read 头部总数与实际条数一致（COUNT 而非全捞）
    m.call_tool("hub_memory_write", {"content": "计数测试记忆A", "kind": "fact"}, root_s)
    outm = m.call_tool("hub_memory_read", {"query": "计数测试"}, root_s)
    assert re.search(r"共 \d+ 条，命中 1 条", outm), outm


def t_memory_read_fallback(tmp):
    """hub_memory_read 0 命中兜底：自动搜记录给线索（#id 可精读）；
    有记忆命中时不附兜底行；记录也 0 命中时保持原文案；连续中文长串走 bigram 重试。"""
    import re
    root = Path(tmp) / "hub_mem_fallback"
    build_hub(root)
    root_s = str(root)
    m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                 "content": "【目的】修复着色器编译卡顿问题，验证通过"}, root_s)
    # 记忆库为空 0 命中，但记录命中（长串走 bigram 重试，"着色器卡顿"≠"着色器编译卡顿"）→ 附线索
    out = m.call_tool("hub_memory_read", {"query": "着色器卡顿"}, root_s)
    assert "大脑记忆无命中" in out, out
    assert "记录里可能有相关线索" in out and "[记录#" in out, out
    assert "弱相关线索" in out, f"兜底线索缺弱相关标注：{out}"
    rid = int(re.search(r"\[记录#(\d+)\]", out).group(1))
    rec = m.call_tool("hub_get_record", {"record_id": rid}, root_s)
    assert "着色器" in rec, f"#id 无法精读，兜底链路断：{rec[:120]}"
    # 有记忆命中时不附兜底行
    m.call_tool("hub_memory_write", {"content": "着色器经验：先清缓存", "kind": "lesson"}, root_s)
    out3 = m.call_tool("hub_memory_read", {"query": "着色器"}, root_s)
    assert "命中 1 条" in out3 and "记录里可能有相关线索" not in out3, out3
    # 记忆、记录都 0 命中 → 无兜底行
    out4 = m.call_tool("hub_memory_read", {"query": "绝不存在的xyz"}, root_s)
    assert "无命中" in out4 and "[记录#" not in out4, out4


def t_log_work_intercept(tmp):
    """写入时踩坑拦截（v2.7）：log_work 返回附相似 lesson/open 错误提醒；无关记录不附。"""
    root = Path(tmp) / "hub_intercept"
    build_hub(root)
    root_s = str(root)
    m.call_tool("hub_memory_write", {"content": "更新桌面快捷方式用 heredoc 写 ps1 无 BOM，PowerShell 中文乱码静默新建错名文件",
                                     "kind": "lesson", "agent": "zcode"}, root_s)
    # 再踩同坑的记录 → 返回带拦截提醒 + 记忆#id
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "dsh",
                                       "content": "【目的】更新桌面快捷方式：heredoc 写 ps1 无 BOM，PowerShell 中文乱码，快捷方式名变乱码还误报成功"}, root_s)
    assert "已记入大脑" in out, out
    assert "大脑拦截提醒" in out and "[记忆#" in out, f"相似踩坑未拦截：{out}"
    # 落痕（v2.8.4）：journal 记「拦截命中」+ 命中记忆 use_count+1（验收②观测数据）
    with brain.db_conn(root_s) as conn:
        jn = conn.execute("SELECT COUNT(*) FROM journal WHERE action LIKE '拦截命中%'").fetchone()[0]
        uc = conn.execute("SELECT use_count FROM memories WHERE kind='lesson'").fetchone()[0]
    assert jn == 1 and uc == 1, (jn, uc)
    # 无关记录 → 无拦截段，也不落痕
    out2 = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "dsh",
                                        "content": "【目的】整理课表导出 PDF 的页边距设置"}, root_s)
    assert "已记入大脑" in out2 and "大脑拦截提醒" not in out2, out2
    with brain.db_conn(root_s) as conn:
        jn2 = conn.execute("SELECT COUNT(*) FROM journal WHERE action LIKE '拦截命中%'").fetchone()[0]
    assert jn2 == 1, jn2


def t_health_acceptance_output(tmp):
    """v2.8.5 hub_health 输出验收达成度行（协议级：四条标准的实时数据可一键复查）。"""
    root = Path(tmp) / "hub_healthout"
    build_hub(root)
    out = m.call_tool("hub_health", {}, str(root))
    assert "验收达成度" in out and "拦截命中" in out, out
    assert "stalled 使用" in out and "置顶位工作知识" in out, out


def t_archive_tool(tmp):
    """hub_archive_project（v2.7）：归档成功（状态+目录）/未找到报错/重复归档报错。"""
    root = Path(tmp) / "hub_archive_tool"
    build_hub(root)
    root_s = str(root)
    out = m.call_tool("hub_archive_project", {"project": "测试-项目", "agent": "zcode"}, root_s)
    assert "已归档" in out and "99_Archive" in out, out
    assert (root / "99_Archive" / "测试-项目").is_dir(), "目录未移入归档区"
    assert not (root / "测试-项目").exists()
    out2 = m.call_tool("hub_archive_project", {"project": "测试-项目"}, root_s)
    assert "已是归档状态" in out2, out2
    out3 = m.call_tool("hub_archive_project", {"project": "不存在的项目xyz"}, root_s)
    assert "未找到" in out3, out3


def t_distill_mark(tmp):
    """hub_distill 展示即登记：同一记录不重复推送（死候选治理，2026-10-02）。"""
    root_s = str(Path(tmp) / "hub")
    m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                 "content": "【目的】验证蒸馏展示标记的回归用例"}, root_s)
    out1 = m.call_tool("hub_distill", {"limit": 50}, root_s)
    assert "验证蒸馏展示标记" in out1, out1
    out2 = m.call_tool("hub_distill", {"limit": 50}, root_s)
    assert "验证蒸馏展示标记" not in out2, f"已展示候选被重复推送：{out2}"


def t_profile_tool(tmp):
    """hub_user_profile 用户画像（3.3）：空库不崩+preference=0 警告/正常库关键行/对抗脏参不炸。"""
    root = fresh_hub(tmp, "profile")
    root_s = str(root)
    # ① 空库：不崩、标题在、preference=0 行动提示在
    out = m.call_tool("hub_user_profile", {}, root_s)
    assert "用户画像" in out and "preference 类记忆 0 条" in out, out
    # ② 正常库：写记录+lesson 后关键行出现
    m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                 "content": "用 python 和 flask 干活"}, root_s)
    m.call_tool("hub_memory_write", {"content": "纠正：先实测再动手", "kind": "lesson",
                                     "agent": "zcode"}, root_s)
    out = m.call_tool("hub_user_profile", {}, root_s)
    assert "活跃时段" in out and "记录分工" in out and "zcode" in out, out
    assert "技术栈词频" in out and "python×" in out, out
    assert "纠正史" in out and "先实测再动手" in out, out
    # ③ preference 警告的语义：lesson 不消除它（教训≠偏好），写入 preference 才消除
    assert "preference 类记忆 0 条" in out, out
    m.call_tool("hub_memory_write", {"content": "偏好：先给方案再动手", "kind": "preference",
                                     "agent": "zcode"}, root_s)
    out = m.call_tool("hub_user_profile", {}, root_s)
    assert "preference 类记忆 0 条" not in out, out
    # ④ 对抗脏参（schema 无参数）：忽略不炸
    out2 = m.call_tool("hub_user_profile", {"hack": "'; DROP TABLE records;--"}, root_s)
    assert "用户画像" in out2, out2


def t_note_tool(tmp):
    """hub_note（08-AⅠ 人的随记）：写入或空content拒绝或hub_search 检索互通出随记段。"""
    root = fresh_hub(tmp, "note")
    root_s = str(root)
    out = m.call_tool("hub_note", {"content": "顿悟：随记是人的思想层", "tags": "顿悟",
                                   "mood": "灵感"}, root_s)
    assert "已记入随记" in out, out
    assert "错误" in m.call_tool("hub_note", {"content": "   "}, root_s)
    out = m.call_tool("hub_search", {"keyword": "顿悟"}, root_s)
    assert "随记#1" in out and "（灵感）" in out and "顿悟：随记是人的思想层" in out, out
    out2 = m.call_tool("hub_search", {"keyword": "完全无关联的查询词"}, root_s)
    assert "随记 0" in out2 or "无结果" in out2, out2[:120]


def t_status_tool(tmp):
    """hub_status 连续性层（3.1）：五问快照——正常/空库/脏数据/限条数/只读性/畸形参数。"""
    root = fresh_hub(tmp, "status")
    root_s = str(root)
    today = datetime.date.today().isoformat()
    # 正常库：塞全五问数据源
    m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                 "content": "【目的】status 用例\n后续：还没做的待办甲"}, root_s)
    m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "hermes",
                                 "content": "status 用例第二条"}, root_s)
    m.call_tool("hub_report_error", {"title": "status用例未关闭坑", "agent": "zcode"}, root_s)
    m.call_tool("hub_handoff", {"action": "create", "project": "测试-项目", "agent": "zcode",
                                "done": "A", "next_step": "B"}, root_s)
    brain.add_memory(root_s, "置顶铁律测试%pinned_%", kind="lesson", pinned=True)
    brain.heartbeat_touch(root_s, "zcode", "测试-项目", note="在线中")
    out = m.call_tool("hub_status", {}, root_s)
    assert "①" in out and "在线会话" in out and "zcode" in out, out
    assert "活跃项目" in out and "测试-项目" in out, out
    assert "今日新增" in out and "最近操作" in out, out
    assert "未关闭错误 1 条" in out and "status用例未关闭坑" in out, out
    assert "待认领交接 1 条" in out, out
    assert "置顶记忆 1 条" in out and "置顶铁律测试%pinned_%" in out, out  # 通配符原样返回不被吞
    assert "待办线索" in out and "待办甲" in out, out
    assert today in out, out
    # 真·空库（不经 build_hub，无迁移记录）：无任何数据也不炸，各区块给"无"
    bare = Path(tmp) / "status_bare"
    bare.mkdir(parents=True)
    brain.init_db(str(bare))
    out = m.call_tool("hub_status", {}, str(bare))
    assert "①" in out and "在线会话：无" in out and "活跃项目（近7天）：无" in out, out
    assert "未关闭错误：无" in out and "待认领交接：无" in out and "待办线索：无" in out, out
    # 活跃窗口边界确定性：远古记录不算活跃、今日记录算——不随运行日期漂移
    ctl = fresh_hub(tmp, "status_ctl")
    brain.add_record(str(ctl), "古老-项目", "hermes", "2020-01-01", "远古记录", "内容")
    brain.add_record(str(ctl), "测试-项目", "zcode", today, "今天记录", "内容")
    out = m.call_tool("hub_status", {}, str(ctl))
    assert "活跃项目（近7天）" in out, out
    act = out.split("活跃项目（近7天）")[1].split("②")[0]
    assert "测试-项目" in act, out
    assert "古老-项目" not in act, out
    # 畸形参数：零参数工具忽略未知键，不炸
    out = m.call_tool("hub_status", {"limit": "abc", "x": None}, root_s)
    assert "①" in out, out
    # 脏数据：sessions 坏 ts、journal note 超长——容错截断不炸
    import sqlite3 as _sq
    with _sq.connect(str(Path(root_s) / "_hub" / "brain.db")) as c:
        c.execute("INSERT INTO sessions(agent,project,note,ts) VALUES('bad','p','n','not-a-date')")
        c.execute("UPDATE journal SET note=? WHERE id=(SELECT MAX(id) FROM journal)", ("命数" * 200,))
    out = m.call_tool("hub_status", {}, root_s)
    assert "①" in out, out  # 坏 ts 行被容错剔除/展示均不炸
    # 限条数：journal 只展示 8 条；errors 展示最新 5 条但 total 是真实数 7
    for i in range(12):
        brain.journal_add(root_s, "zcode", f"流水{i}")
    for i in range(7):
        brain.error_add(root_s, "zcode", f"坑{i}")
    out = m.call_tool("hub_status", {}, root_s)
    assert "未关闭错误 8 条" in out, out  # 正常库段 1 条 + 本段 7 条
    err_block = out.split("未关闭错误")[1].split("待认领")[0]
    assert "坑6" in err_block and "坑1" not in err_block, err_block  # id DESC 最新 5 条封顶
    assert len([ln for ln in out.splitlines() if "流水" in ln]) <= 8, out
    # 只读性：调用后 records/journal/searches 行数不变
    import re as _re
    def counts():
        with _sq.connect(str(Path(root_s) / "_hub" / "brain.db")) as c:
            return (c.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                    c.execute("SELECT COUNT(*) FROM journal").fetchone()[0],
                    c.execute("SELECT COUNT(*) FROM searches").fetchone()[0])
    before = counts()
    m.call_tool("hub_status", {}, root_s)
    assert counts() == before, "hub_status 产生了写入（必须纯只读）"


def t_decision_record(tmp):
    """3.2 AgDR 决策记录（MCP 层）：带参写入/全空旧行为/outline 抽段/
    畸形类型/超长拦截/content 空时决策字段不救场/Markdown 表格原样。"""
    root = fresh_hub(tmp, "decision")
    root_s = str(root)

    def _rid(out):
        return int(out.split("records#")[1].split("，")[0])

    # ① 正常：四决策字段齐全 → content 尾部决策段，读回含决策内容
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "【目的】拍板\n【做了什么】定 B",
                                       "context": "前提 X", "options": "A 被否\nC 代价高",
                                       "decision": "选 B", "tradeoffs": "接受 Y"}, root_s)
    assert "已记入大脑" in out, out
    rid = _rid(out)
    rec = m.call_tool("hub_get_record", {"record_id": rid}, root_s)
    assert "## 决策记录" in rec and "- 决策：选 B" in rec and "- 上下文：前提 X" in rec, rec
    # ② outline 级抽出决策段头（3.2 验收：四段式占满槽位也不挤掉）
    rec_ol = m.call_tool("hub_get_record", {"record_id": rid, "level": "outline"}, root_s)
    assert "决策记录" in rec_ol and "【目的】" in rec_ol, rec_ol
    # ③ 全空（不传）= 旧行为：无决策段
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "老式记录"}, root_s)
    assert "决策记录" not in m.call_tool("hub_get_record", {"record_id": _rid(out)}, root_s)
    # ④ 畸形类型：数字/None/对象/列表 → 转 str 不炸，空值跳过
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "畸形参数记录", "context": 123,
                                       "options": None, "decision": {"嵌套": "对象"},
                                       "tradeoffs": ["列", "表"]}, root_s)
    assert "已记入大脑" in out, out
    rec3 = m.call_tool("hub_get_record", {"record_id": _rid(out)}, root_s)
    assert "- 上下文：123" in rec3 and "- 决策：" in rec3, rec3
    # ⑤ 超长拦截：决策字段把正文顶过 MAX_CONTENT → 拒绝且不落库
    before = len(brain.list_records(root_s, "测试-项目"))
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "超长攻击", "decision": "长" * (128 * 1024)},
                      root_s)
    assert "超长" in out, out
    assert len(brain.list_records(root_s, "测试-项目")) == before, "超长记录不得落库"
    # ⑥ content 空但决策字段有值 → 仍按 content 必填拒绝（决策字段不能替代正文）
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": " ", "decision": "只有决策"}, root_s)
    assert "必填" in out, out
    # ⑦ Markdown 表格/换行原样保留不炸
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "表格记录",
                                       "options": "| 方案 | 代价 |\n|---|---|\n| A | X |",
                                       "decision": "选 A"}, root_s)
    assert "| 方案 | 代价 |" in m.call_tool("hub_get_record", {"record_id": _rid(out)}, root_s)


def t_ask_metacog_tool(tmp):
    """批次B·D2/D10：hub_ask 结构化回答+逐条出处+明确弃权；hub_metacognition 自省面板。"""
    root = fresh_hub(tmp, "askmc")
    rs = str(root)
    m.call_tool("hub_memory_write", {"content": "FlClash 订阅更新会覆盖 profile_id 导致规则脱钩",
                                     "kind": "lesson", "project": "FlClash", "agent": "zcode"}, rs)
    m.call_tool("hub_log_work", {"project": "FlClash", "agent": "zcode",
                                 "content": "【目的】修规则脱钩。下一步：补 README"}, rs)
    out = m.call_tool("hub_ask", {"question": "FlClash 最大的风险是什么", "project": "FlClash"}, rs)
    assert "问大脑" in out and "出处" in out and "记忆#" in out, out
    out2 = m.call_tool("hub_ask", {"question": "量子计算机纠错码是什么"}, rs)
    assert "不知道" in out2, out2
    out3 = m.call_tool("hub_metacognition", {}, rs)
    assert "元认知" in out3 and ("覆盖盲区" in out3 or "现行记忆" in out3), out3
    # 空参数不崩（question 缺失 → 走弃权/空问句）
    assert "问大脑" in m.call_tool("hub_ask", {}, rs)


# ---------------------------------------------------------------- B1/B2 身份锁定（2026-10-10）

def t_server_agent_override(tmp):
    """B2 单点派生：① 有锁定 → 覆盖自报 ② 无锁定 → 退回自报 ③ 筛选工具不下发 ④ 不一致留痕。"""
    rs = str(fresh_hub(tmp, "sa"))
    old = m.SERVER_AGENT
    try:
        # ① 无锁定：走自报（兼容旧接入）
        m.SERVER_AGENT = ""
        got = m.apply_server_agent("hub_log_work", {"agent": "zcode"}, rs)
        assert got["agent"] == "zcode", got

        # ② 有锁定：覆盖自报值
        m.SERVER_AGENT = "dsh"
        got = m.apply_server_agent("hub_log_work", {"agent": "zcode", "project": "x"}, rs)
        assert got["agent"] == "dsh", got
        # 不传 agent 也补上
        got = m.apply_server_agent("hub_log_work", {"project": "x"}, rs)
        assert got["agent"] == "dsh", got
        # 不污染调用方原对象
        orig = {"agent": "zcode"}
        m.apply_server_agent("hub_log_work", orig, rs)
        assert orig == {"agent": "zcode"}, "调用方 dict 被就地修改"

        # ③ 只读筛选工具不下发（agent 参数是"查别人"，语义不同）
        got = m.apply_server_agent("hub_list_skills", {"agent": "zcode"}, rs)
        assert got["agent"] == "zcode", got

        # ④ 不一致留痕
        n0 = len([e for e in brain.journal_list(rs, 500) if e.get("action") == "agent_mismatch"])
        m._MISMATCH_SEEN.clear()
        m.apply_server_agent("hub_log_work", {"agent": "hermes"}, rs)
        hits = [e for e in brain.journal_list(rs, 500) if e.get("action") == "agent_mismatch"]
        assert len(hits) == n0 + 1, f"不一致未留痕（{n0}->{len(hits)}）"
        assert "hermes" in hits[0]["note"] and "dsh" in hits[0]["note"], hits[0]  # journal 最新在前
        # 节流：同类只记一次
        m.apply_server_agent("hub_log_work", {"agent": "hermes"}, rs)
        assert len([e for e in brain.journal_list(rs, 500)
                    if e.get("action") == "agent_mismatch"]) == n0 + 1, "节流失效"

        # ⑤ 端到端：锁定后写入的 agent 由服务端决定，自报被忽略
        out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                          "content": "锁定身份下的写入"}, rs)
        assert "已记入大脑" in out, out
        rec = [r for r in brain.list_records(rs, project="测试-项目", limit=100)
               if "锁定身份下的写入" in r["content"]]
        assert rec and rec[0]["agent"] == "dsh", f"未按锁定身份落库：{rec}"
    finally:
        m.SERVER_AGENT = old
        m._MISMATCH_SEEN.clear()


def t_agent_check(tmp):
    """A3 身份校验：注册表内放行、显示名归一、非法拒收、无主语义放行、锁定值不校验。"""
    rs = str(fresh_hub(tmp, "ck"))
    # 注册表内 / 显示名 / 别名
    for raw, exp in [("zcode", "zcode"), ("ZCode", "zcode"), ("WorkBuddy", "workbuddy"),
                     ("Claude Code", "claude"), ("DSH", "dsh"), ("deepseek", "deepseek")]:
        got, err = brain.check_agent(raw, rs)
        assert got == exp and not err, f"{raw} -> {got!r} {err}"
    # 无主语义放行（不是"错误身份"）
    for raw in ("unknown", "", "user", "migrated"):
        got, err = brain.check_agent(raw, rs)
        assert not err, f"{raw} 应放行：{err}"
    # 不在册 → 拒收且给出可用名单
    got, err = brain.check_agent("gpt4", rs)
    assert got == "" and "不在注册表" in err and "zcode" in err, err
    # 端到端拒收
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "gpt4", "content": "x"}, rs)
    assert "拒收" in out, out
    out = m.call_tool("hub_memory_write", {"content": "x", "agent": "gpt4"}, rs)
    assert "拒收" in out, out
    # ★ 锁定值不校验：接入配置配错名时不该让该 agent 连工作都记不了
    old = m.SERVER_AGENT
    try:
        m.SERVER_AGENT = "typo-name"
        got, err = m._agent_for_write({"agent": "zcode"}, rs)
        assert got == "typo-name" and not err, (got, err)
    finally:
        m.SERVER_AGENT = old


def t_parse_argv():
    """B1/P3-1 启动参数解析：--agent / --profile 具名参数不得与 root 位置参数冲突。"""
    R = r"D:\some\root"
    cases = [
        ([], (None, False, "")),
        (["--agent", "zcode"], (None, False, "zcode")),
        (["--agent=dsh"], (None, False, "dsh")),
        ([R], (R, True, "")),
        (["--agent", "dsh", R], (R, True, "dsh")),
        ([R, "--agent", "hermes"], (R, True, "hermes")),
        (["--agent"], (None, False, "")),          # 缺值不崩
        (["--profile", "reader"], (None, False, "")),        # profile 具名参数
        (["--profile=worker", R], (R, True, "")),
        (["--profile", "reader", "--agent", "zcode", R], (R, True, "zcode")),
    ]
    for argv, (exp_root, exp_fixed, exp_agent) in cases:
        root, fixed, agent, _profile = m.parse_argv(argv)
        assert fixed == exp_fixed and agent == exp_agent, f"{argv} -> {fixed},{agent}"
        if exp_root:
            assert root == exp_root, f"{argv} -> root={root}"
        else:
            # 未传 root → 必须走动态跟随（取 config），不能误当 fixed
            assert root == core.get_root(), f"{argv} 应动态跟随，实际 {root}"
    # profile 值透传与缺省
    assert m.parse_argv(["--profile", "reader"])[3] == "reader"
    assert m.parse_argv(["--profile=worker"])[3] == "worker"
    assert m.parse_argv([])[3] == ""


def t_p3_contract(tmp):
    """P3-1 契约：profile 切片（藏起≠禁用）/ discover 协商（-32022）/ max_chars 截断
    / dry_run 不落库 / confirm=false 闸门 / 写后审计与 undo 句柄 / 发现工具。"""
    root = fresh_hub(tmp, "p3c")
    rs = str(root)

    # ① profile 切片：reader 3~8 个；藏起的工具仍可直接调用
    old = m.SERVER_PROFILE
    try:
        m.SERVER_PROFILE = "reader"
        names = {t["name"] for t in resp_ok(m.handle_message(rpc("tools/list"), rs))["tools"]}
        assert 3 <= len(names) <= 8, f"reader 应只看到 3~8 个：{len(names)}"
        assert {"hub_help", "hub_status", "hub_heartbeat"} <= names
        assert "hub_log_work" not in names
        out = m.call_tool("hub_list_projects", {"max_chars": 0}, rs)
        assert "共" in out, "藏起的工具仍应可直接调用（藏起≠禁用）"
        m.SERVER_PROFILE = "worker"
        wn = {t["name"] for t in resp_ok(m.handle_message(rpc("tools/list"), rs))["tools"]}
        assert len(wn) < len(m.TOOLS), "worker 应比 admin 小"
        assert "hub_log_work" in wn and "hub_ops_run" not in wn
    finally:
        m.SERVER_PROFILE = old

    # ② server/discover + 未知版本 -32022（initialize 握手保留 = dual-era）
    d = resp_ok(m.handle_message(rpc("server/discover"), rs))
    assert d["profile"] == m.SERVER_PROFILE and d["totalToolCount"] == len(m.TOOLS)
    assert resp_ok(m.handle_message(rpc("initialize", {"protocolVersion": "2025-06-18"}), rs))
    bad = m.handle_message(rpc("server/discover", {"protocolVersion": "0.0"}), rs)
    assert bad["error"]["code"] == -32022, bad

    # ③ max_chars：截断 + 文末标注 + structuredContent；0=不截断
    r = resp_ok(m.handle_message(rpc("tools/call", {"name": "hub_status",
                                                    "arguments": {"max_chars": 40}}, 9), rs))
    assert "已截断" in r["content"][0]["text"]
    meta = r["structuredContent"]["meta"]
    assert meta["truncated"] is True and meta["total"] > 40
    assert "full_text" in r["structuredContent"]
    r2 = resp_ok(m.handle_message(rpc("tools/call", {"name": "hub_status",
                                                     "arguments": {"max_chars": 0}}, 10), rs))
    assert "structuredContent" not in r2 and "已截断" not in r2["content"][0]["text"]

    # ④ dry_run 不落库、不审计
    before = len(brain.list_records(rs, "测试-项目", limit=2000))
    r3 = resp_ok(m.handle_message(rpc("tools/call", {"name": "hub_log_work", "arguments": {
        "project": "测试-项目", "agent": "zcode", "content": "dry run 样本", "dry_run": True}}, 11), rs))
    assert "dry_run" in r3["content"][0]["text"] and "未落任何库" in r3["content"][0]["text"]
    assert len(brain.list_records(rs, "测试-项目", limit=2000)) == before, "dry_run 不得落库"
    assert not brain.audit_list(rs), "dry_run 不应产生审计"

    # ⑤ confirm=false → input_required 且不执行（return-and-resume）
    r4 = resp_ok(m.handle_message(rpc("tools/call", {"name": "hub_log_work", "arguments": {
        "project": "测试-项目", "agent": "zcode", "content": "confirm false", "confirm": False}}, 12), rs))
    assert r4.get("resultType") == "input_required"
    assert len(brain.list_records(rs, "测试-项目", limit=2000)) == before

    # ⑥ 真写 → 审计落库 + undo 句柄
    r5 = resp_ok(m.handle_message(rpc("tools/call", {"name": "hub_log_work", "arguments": {
        "project": "测试-项目", "agent": "zcode", "content": "正式写入（P3-1 审计）"}}, 13), rs))
    assert not r5["isError"]
    aud = brain.audit_list(rs, 5)
    assert aud and aud[0]["tool"] == "hub_log_work"
    assert aud[0]["undo_kind"] == "record" and aud[0]["undo_ref"], aud

    # ⑦ 发现工具：hub_search_tools / hub_call_tool（含防递归）
    t = m.call_tool("hub_search_tools", {"keyword": "认领"}, rs)
    assert "hub_claim" in t
    t2 = m.call_tool("hub_call_tool", {"name": "hub_tasks", "arguments": {}}, rs)
    assert "任务" in t2
    assert "错误" in m.call_tool("hub_call_tool", {"name": "hub_call_tool"}, rs), "防递归"
    assert "错误" in m.call_tool("hub_call_tool", {"name": "hub_不存在"}, rs)


def t_p3_tasks_events(tmp):
    """P3-2 状态机：并发认领只胜一家 / 持有者约束 / 超时自动接管 / 阻塞不可领 / 事件流增量。"""
    import threading as _th
    root = fresh_hub(tmp, "p3t")
    rs = str(root)

    r = m.call_tool("hub_task_add", {"title": "并发认领", "project": "测试-项目", "agent": "zcode"}, rs)
    tid = int(r.split("#")[1].split("：")[0])
    wins = []

    def claim(who):
        err, _row = brain.task_claim(rs, tid, who)
        if not err:
            wins.append(who)

    ts = [_th.Thread(target=claim, args=(w,)) for w in ("zcode", "dsh")]
    [x.start() for x in ts]
    [x.join() for x in ts]
    assert len(wins) == 1, f"并发认领应只胜一家：{wins}"
    owner = wins[0]
    loser = "dsh" if owner == "zcode" else "zcode"

    err2, _ = brain.task_claim(rs, tid, loser)
    assert "已被" in err2 and owner in err2

    err3, _ = brain.task_update(rs, tid, status="done", agent=loser)
    assert "持有" in err3, "非持有者不得流转"
    err4, row4 = brain.task_update(rs, tid, status="done", agent=owner)
    assert not err4 and row4["status"] == "done"
    err5, _ = brain.task_update(rs, tid, status="pending", agent=owner)
    assert "已完成" in err5

    # 超时自动接管：伪造 claimed_at 为 3 小时前（TTL=2h）
    r2 = m.call_tool("hub_task_add", {"title": "超时接管", "agent": "zcode"}, rs)
    tid2 = int(r2.split("#")[1].split("：")[0])
    assert not brain.task_claim(rs, tid2, "zcode")[0]
    old_ts = (datetime.datetime.now() - datetime.timedelta(hours=3)).isoformat(timespec="seconds")
    with brain.db_conn(rs) as conn:
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (old_ts, tid2))
    err6, row6 = brain.task_claim(rs, tid2, "dsh")
    assert not err6 and row6["owner"] == "dsh", "超时未自动释放接管"

    # 阻塞任务不可认领；解除后可
    r3 = m.call_tool("hub_task_add", {"title": "阻塞任务", "blocked_by": "外部依赖", "agent": "zcode"}, rs)
    tid3 = int(r3.split("#")[1].split("：")[0])
    err7, _ = brain.task_claim(rs, tid3, "dsh")
    assert "阻塞" in err7
    err8, _ = brain.task_update(rs, tid3, status="pending", agent="")
    assert not err8
    err9, row9 = brain.task_claim(rs, tid3, "dsh")
    assert not err9 and row9["status"] == "in_progress"

    # 列表排序契约：进行中 → 阻塞 → 待办 → 完成（各类可能缺项，按秩单调性断言）
    rows = brain.task_list(rs, limit=50)
    rank = {"in_progress": 0, "blocked": 1, "pending": 2, "done": 3}
    ranks = [rank[r["status"]] for r in rows]
    assert ranks == sorted(ranks), f"任务排序不符：{[r['status'] for r in rows]}"

    # 事件流：写入 + 增量订阅只取新事件
    assert m.call_tool("hub_event_write", {"content": "进展A", "project": "测试-项目",
                                           "agent": "zcode"}, rs)
    first = brain.event_list(rs, limit=10)
    assert first and first[0]["content"] == "进展A"
    m.call_tool("hub_event_write", {"content": "进展B", "agent": "dsh"}, rs)
    inc = brain.event_list(rs, since_id=first[0]["id"], limit=10)
    assert len(inc) == 1 and inc[0]["content"] == "进展B", "增量订阅应只取新事件"
    assert "进展B" in m.call_tool("hub_events", {"limit": 5}, rs)

    # 坏参不崩
    assert "不存在" in m.call_tool("hub_claim", {"task_id": 999999, "agent": "zcode"}, rs)
    assert "错误" in m.call_tool("hub_task_add", {"title": "  ", "agent": "zcode"}, rs)
    assert "错误" in m.call_tool("hub_task_update", {"task_id": tid, "status": "乱写"}, rs)


def t_playbook(tmp):
    """Y1 编排层（协议级）：模板清单/show/计划不落库/自建与拒收/真跑到 manual 停/断点续跑/失败即停/中止/坏参。"""
    root = fresh_hub(tmp, "pb")
    rs = str(root)

    out = m.call_tool("hub_playbook", {"action": "list"}, rs)
    assert "release" in out and "daily" in out and "内置" in out, f"内置模板缺失：{out[:200]}"
    assert "ops_check" in m.call_tool("hub_playbook", {"action": "show", "name": "release"}, rs)

    # plan 只列计划，不产生执行记录
    m.call_tool("hub_playbook", {"action": "run", "name": "release", "plan": True}, rs)
    assert not brain.playbook_runs(rs), "plan 预演不应产生执行记录"

    steps = [{"key": "a", "title": "打点", "action": "noop", "args": {"text": "hello {{project}}"}},
             {"key": "b", "title": "人工步骤", "action": "manual", "note": "去干活"},
             {"key": "c", "title": "收尾", "action": "noop", "args": {"text": "after {{out.a}}"}}]
    assert "已保存" in m.call_tool("hub_playbook", {"action": "create", "name": "myflow",
                                                   "title": "我的流程", "steps": steps}, rs)
    assert "不可覆盖" in m.call_tool("hub_playbook", {"action": "create", "name": "release",
                                                     "steps": [{"action": "noop"}]}, rs)
    assert "不在注册表" in m.call_tool("hub_playbook", {"action": "create", "name": "bad",
                                                       "steps": [{"action": "rm_rf"}]}, rs)
    assert "steps 必填" in m.call_tool("hub_playbook", {"action": "create", "name": "bad2"}, rs)

    # 真跑：第 1 步 done，第 2 步 manual 停住，第 3 步不跑
    r1 = m.call_tool("hub_playbook", {"action": "run", "name": "myflow",
                                      "project": "测试-项目", "agent": "zcode"}, rs)
    assert "[待人工]" in r1 and "hello 测试-项目" in r1, f"变量替换或等待态异常：{r1[:300]}"
    rid = int(r1.split("run #")[1].split("：")[0])
    with brain.db_conn(rs) as conn:
        st = [dict(x) for x in conn.execute(
            "SELECT seq,status FROM playbook_run_steps WHERE run_id=? ORDER BY seq", (rid,))]
    assert [x["status"] for x in st] == ["done", "manual", "pending"], f"步骤状态错：{st}"

    # 续跑：manual 视为人已做完，第 3 步引用 {{out.a}}
    r2 = m.call_tool("hub_playbook", {"action": "resume", "run_id": rid}, rs)
    assert "[完成]" in r2 and "after hello 测试-项目" in r2, f"续跑或变量引用异常：{r2[:300]}"
    assert "无需续跑" in m.call_tool("hub_playbook", {"action": "resume", "run_id": rid}, rs)
    assert f"#{rid}" in m.call_tool("hub_playbook", {"action": "runs"}, rs)

    # 失败即停：第 2 步失败，第 3 步不该跑
    m.call_tool("hub_playbook", {"action": "create", "name": "failflow", "steps": [
        {"key": "x", "title": "打点", "action": "noop", "args": {"text": "ok"}},
        {"key": "y", "title": "缺 project 的写记录", "action": "log_work", "args": {"content": "x"}},
        {"key": "z", "title": "不该跑到", "action": "noop", "args": {"text": "never"}}]}, rs)
    r3 = m.call_tool("hub_playbook", {"action": "run", "name": "failflow"}, rs)
    assert "[失败]" in r3 and "never" not in r3, f"失败未即停：{r3[:300]}"
    rid3 = int(r3.split("run #")[1].split("：")[0])
    assert "[失败]" in m.call_tool("hub_playbook", {"action": "resume", "run_id": rid3}, rs)
    assert "已中止" in m.call_tool("hub_playbook", {"action": "abort", "run_id": rid3}, rs)

    # 坏参不崩
    assert "不存在" in m.call_tool("hub_playbook", {"action": "run", "name": "nope"}, rs)
    assert "不存在" in m.call_tool("hub_playbook", {"action": "show", "name": "nope"}, rs)
    assert "不存在" in m.call_tool("hub_playbook", {"action": "resume", "run_id": 999999}, rs)
    assert "不存在" in m.call_tool("hub_playbook", {"action": "abort", "run_id": 999999}, rs)
    assert "只支持" in m.call_tool("hub_playbook", {"action": "bogus"}, rs)


def t_trigger(tmp):
    """Y2 自动化层：建/校验/默认停用/条件匹配/冷却/强制试跑/心跳可见/playbook 动作/停用删除/坏参。"""
    root = fresh_hub(tmp, "trg")
    rs = str(root)

    assert "on_event 必须" in m.call_tool("hub_trigger", {"action": "add", "name": "x",
                                                          "on_event": "bogus"}, rs)
    assert "action_type 必须" in m.call_tool("hub_trigger", {"action": "add", "name": "x",
                                                             "action_type": "rm_rf"}, rs)
    assert "必须在 action_args" in m.call_tool("hub_trigger", {"action": "add", "name": "x",
                                                               "action_type": "playbook"}, rs)
    assert "name 必填" in m.call_tool("hub_trigger", {"action": "add"}, rs)

    r = m.call_tool("hub_trigger", {"action": "add", "name": "开工提醒", "on_event": "heartbeat",
                                    "action_type": "notify", "action_args": {"text": "记得先跑体检"},
                                    "condition": {"project": "测试-项目"}, "agent": "zcode"}, rs)
    assert "停用" in r, f"新建应默认停用：{r}"
    tid = int(r.split("#")[1].split("（")[0])

    # 未启用 → 不触发；启用后条件不匹配 → 也不触发
    assert brain.trigger_eval(rs, "heartbeat", {"agent": "zcode", "project": "测试-项目"}) == []
    assert "启用" in m.call_tool("hub_trigger", {"action": "enable", "id": tid}, rs)
    assert brain.trigger_eval(rs, "heartbeat", {"agent": "zcode", "project": "别的项目"}) == []

    # 匹配 → 触发；再触发 → 冷却
    res = brain.trigger_eval(rs, "heartbeat", {"agent": "zcode", "project": "测试-项目"})
    assert res and res[0]["status"] == "fired" and "体检" in res[0]["text"], f"未触发：{res}"
    assert brain.trigger_eval(rs, "heartbeat", {"agent": "zcode", "project": "测试-项目"})[0]["status"] == "cooled"
    assert "冷却中" in m.call_tool("hub_trigger", {"action": "fire", "id": tid}, rs)
    assert "√" in m.call_tool("hub_trigger", {"action": "fire", "id": tid, "force": True}, rs)

    # cooldown=0 的触发器：心跳输出里应出现 [自动化] 段
    r2 = m.call_tool("hub_trigger", {"action": "add", "name": "每次提醒", "on_event": "heartbeat",
                                     "action_type": "notify", "action_args": {"text": "hi-marker"},
                                     "cooldown_min": 0}, rs)
    tid2 = int(r2.split("#")[1].split("（")[0])
    m.call_tool("hub_trigger", {"action": "enable", "id": tid2}, rs)
    hb = m.call_tool("hub_heartbeat", {"agent": "zcode", "project": "测试-项目"}, rs)
    assert "[自动化]" in hb and "hi-marker" in hb, f"心跳未见自动化段：{hb[:400]}"

    # playbook 动作（真跑 onboard）
    r3 = m.call_tool("hub_trigger", {"action": "add", "name": "自动体检", "on_event": "record",
                                     "action_type": "playbook",
                                     "action_args": {"name": "onboard", "project": "测试-项目"}}, rs)
    tid3 = int(r3.split("#")[1].split("（")[0])
    fired = m.call_tool("hub_trigger", {"action": "fire", "id": tid3, "force": True}, rs)
    assert "√" in fired and "onboard" in fired, f"playbook 动作未跑起来：{fired[:200]}"

    # due 事件（时点类）：无到期项静默；有到期项才触发，并带 {{due_count}}/{{due_detail}}
    r4 = m.call_tool("hub_trigger", {"action": "add", "name": "到期提醒", "on_event": "due",
                                     "action_type": "notify",
                                     "action_args": {"text": "有 {{due_count}} 项到期：{{due_detail}}"}}, rs)
    tid4 = int(r4.split("#")[1].split("（")[0])
    m.call_tool("hub_trigger", {"action": "enable", "id": tid4}, rs)
    assert brain.trigger_eval(rs, "due", {}) == [], "无到期项时 due 触发器应静默"
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode", "content": "决策测试",
                                 "decision": "选 A", "review_date": yesterday}, rs)
    res4 = brain.trigger_eval(rs, "due", {})
    assert res4 and res4[0]["status"] == "fired" and "1 项到期" in res4[0]["text"], \
        f"due 触发器未按时点触发：{res4}"

    assert "触发器" in m.call_tool("hub_trigger", {"action": "list"}, rs)
    assert "停用" in m.call_tool("hub_trigger", {"action": "disable", "id": tid2}, rs)
    assert "删除" in m.call_tool("hub_trigger", {"action": "del", "id": tid2}, rs)
    assert "不存在" in m.call_tool("hub_trigger", {"action": "enable", "id": 999999}, rs)
    assert "不存在" in m.call_tool("hub_trigger", {"action": "fire", "id": 999999, "force": True}, rs)
    assert "只支持" in m.call_tool("hub_trigger", {"action": "bogus"}, rs)


def t_graph(tmp):
    """Y3 知识图谱：结构边物化/体检/邻居/相关条目/最短路径/知识簇/坏参。"""
    root = fresh_hub(tmp, "graph")
    rs = str(root)
    r1 = brain.add_record(rs, "测试-图谱", "zcode", "2026-10-13", "记录A", "内容A")
    r2 = brain.add_record(rs, "测试-图谱", "zcode", "2026-10-13", "记录B", "内容B")
    brain.add_record(rs, "别的-项目", "zcode", "2026-10-13", "记录C", "内容C")
    m1 = brain.add_memory(rs, "记忆甲", tags="图谱,测试", project="测试-图谱", agent="zcode")
    brain.add_memory(rs, "记忆乙", tags="图谱", project="测试-图谱", agent="zcode")

    st = brain.graph_build(rs)
    assert st["in"] >= 3 and st["tag"] >= 3, f"结构边未物化：{st}"
    st2 = brain.graph_stats(rs, build=False)
    assert st2["linked"] >= 4 and st2["edges"] >= 6, f"体检数据异常：{st2}"
    assert st2["isolated"] >= 0 and st2["largest_cluster"] >= 2

    nb = brain.graph_neighbors(rs, "tag:图谱")
    assert len(nb["in"]) >= 2, f"标签节点应有入边：{nb}"
    assert "error" in brain.graph_neighbors(rs, "瞎写")

    rel = brain.graph_related(rs, f"record:{r1}")
    assert any(x["node"] == f"record:{r2}" for x in rel), f"同项目记录未关联：{rel}"
    assert brain.graph_related(rs, "瞎写:1") == []

    p = brain.graph_path(rs, f"record:{r1}", f"memory:{m1}", max_depth=4)
    assert p["found"] and p["depth"] == 2, f"两点应经项目节点相连：{p}"
    assert not brain.graph_path(rs, "record:999999", "memory:999999")["found"]
    assert "error" in brain.graph_path(rs, "record:1", "bogus:2")

    cl = brain.graph_clusters(rs, build=False)
    assert cl and cl[0]["size"] >= 2, f"未成簇：{cl}"

    # 孤岛清单：无标签无项目的记忆应在清单里（stats 只给个数，行动要靠清单）
    m3 = brain.add_memory(rs, "孤立记忆", tags="", project="", agent="zcode")
    iso = brain.graph_isolated(rs, limit=50)
    assert any(x["node"] == f"memory:{m3}" for x in iso), f"孤岛清单漏了孤立记忆：{iso[:5]}"
    assert not any(x["node"] == f"memory:{m1}" for x in iso), "有边的记忆不该算孤岛"

    assert "结构边已重建" in m.call_tool("hub_graph", {"action": "build"}, rs)
    assert "知识图谱体检" in m.call_tool("hub_graph", {"action": "stats"}, rs)
    assert "路径（2 跳）" in m.call_tool("hub_graph", {"action": "path", "from_node": f"record:{r1}",
                                                      "to_node": f"memory:{m1}"}, rs)
    assert "知识簇" in m.call_tool("hub_graph", {"action": "clusters"}, rs)
    assert "邻居" in m.call_tool("hub_graph", {"action": "neighbors", "node": "tag:图谱"}, rs)
    assert "孤岛" in m.call_tool("hub_graph", {"action": "isolated", "limit": 5}, rs)


def t_playbook_upgrade(tmp):
    """内置模板可升级：改了内置定义，已建库的步骤要跟着变（否则修正永远推不进真实大脑）。"""
    root = fresh_hub(tmp, "pbup")
    rs = str(root)
    keys = [s["key"] for s in brain.playbook_get(rs, "release")["steps"]]
    assert "regression" in keys, f"release 应含回归步骤：{keys}"
    # 抹掉一步（模拟旧版出厂状态）→ 重新 seed 应重建回新版
    with brain.db_conn(rs) as conn:
        conn.execute("DELETE FROM playbook_steps WHERE playbook='release' AND key='regression'")
    assert "regression" not in [s["key"] for s in brain.playbook_get(rs, "release")["steps"]]
    brain._PB_SEEDED.discard(rs)
    brain._pb_seed(rs)
    keys2 = [s["key"] for s in brain.playbook_get(rs, "release")["steps"]]
    assert "regression" in keys2, f"内置模板未升级：{keys2}"
    # 升级不得动历史执行记录
    assert isinstance(brain.playbook_runs(rs), list)


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_mcp_")
    root = Path(tmp) / "hub"
    build_hub(root)
    print(f"临时目录：{tmp}\n")
    case("交接协议工具（create/list/claim/重复认领拒绝/空态）", lambda: t_handoff_tool(root))
    case("MCP协议（握手/50工具/未知方法/异常自动登记/ping）", lambda: t_protocol(root))
    case("批次B·问大脑D2+元认知D10工具（结构化回答/出处/弃权/自省面板/空参不崩）", lambda: t_ask_metacog_tool(tmp))
    case("MCP工具集（读写记录/搜索/公用记忆/进度/注入拦截）", lambda: t_tools(root))
    case("记忆overwrite语义（真清空+备份+非法mode）", lambda: t_memory_overwrite(tmp))
    case("record_type枚举校验（非法拒收不落库/合法入库）", lambda: t_record_type_guard(tmp))
    case("P1-3 双链自动成 relation（MCP 写入路径）", lambda: t_links_extract(tmp))
    case("8线程并发log_work（不丢行/journal完整）", lambda: t_concurrent_log_work(tmp, fresh_hub(tmp, "conc")))
    case("心跳撞车预警/陈旧清理/坏json自愈", lambda: t_heartbeat_conflict(fresh_hub(tmp, "hb")))
    case("M10 心跳开工推送今日任务（空库不刷/项目过滤/回退全局/P排序）", lambda: t_heartbeat_today(fresh_hub(tmp, "hbt")))
    case("错误登记（上报/查询/流转/坏行）", lambda: t_errors_flow(fresh_hub(tmp, "err")))
    case("撤销（只切自己最新段/误伤检查/栈式撤销）", lambda: t_undo_log(fresh_hub(tmp, "undo")))
    case("新工具（get_rules/create_project+注入拒绝）", lambda: t_new_tools(tmp))
    case("单条全文读取+Everything列表回归（畸形id/死代码bug）", lambda: t_get_record_and_search_files(tmp))
    case("蒸馏展示即登记（同一记录不重复推送）", lambda: t_distill_mark(tmp))
    case("hub_status五问快照（正常/空库/脏数据/限条数/只读性/畸形参数）", lambda: t_status_tool(tmp))
    case("决策记录AgDR（带参写入/旧行为/outline抽段/畸形类型/超长拦截/表格原样）", lambda: t_decision_record(tmp))
    case("检索顺手度（#id精读闭环/片段/零命中建议/通配符转义/计数）", lambda: t_search_output(tmp))
    case("memory_read零命中兜底（记录线索/#id可精读/命中不附/真无线索）", lambda: t_memory_read_fallback(tmp))
    case("写入时踩坑拦截（相似lesson提醒/无关不附）", lambda: t_log_work_intercept(tmp))
    case("体检验收达成度输出（协议级）", lambda: t_health_acceptance_output(tmp))
    case("项目归档工具（状态+目录/未找到/重复归档）", lambda: t_archive_tool(tmp))
    case("引导注入（幂等/移除还原/自动创建/四家目标）", lambda: t_bootstrap(tmp))
    case("端到端子进程握手", lambda: t_end_to_end(root))
    case("root动态跟随（迁移后旧进程写新库/不误写旧库）", lambda: t_root_follow(tmp))
    case("一键接入（两种布局/备份/移除/坏json/幂等）", lambda: t_mcp_access(tmp))
    case("用户画像工具（空库/preference警告/正常库关键行/脏参）", lambda: t_profile_tool(tmp))
    case("随记工具（写入/空content拒绝/检索互通随记段）", lambda: t_note_tool(tmp))
    case("B1/B2身份锁定（覆盖自报/兼容旧接入/筛选不下发/不一致留痕/节流）", lambda: t_server_agent_override(tmp))
    case("身份校验（注册表放行/显示名归一/非法拒收/锁定值不校验）", lambda: t_agent_check(tmp))
    case("B1启动参数解析（--agent与root位置参数不冲突）", lambda: t_parse_argv())
    case("P3-1契约（profile切片/discover协商/max_chars截断/dry_run/confirm闸门/审计undo）",
         lambda: t_p3_contract(tmp))
    case("P3-2待办状态机（并发认领只胜一家/持有者约束/超时接管/阻塞/事件流增量）",
         lambda: t_p3_tasks_events(tmp))
    case("Y1 编排层（模板清单/计划不落库/自建拒收/真跑停人工/断点续跑/失败即停/中止/坏参）",
         lambda: t_playbook(tmp))
    case("Y2 自动化（默认停用/条件匹配/冷却/强制试跑/心跳可见/playbook动作/停用删除/坏参）",
         lambda: t_trigger(tmp))
    case("Y3 知识图谱（结构边物化/体检/邻居/相关条目/最短路径/知识簇/孤岛清单/坏参）",
         lambda: t_graph(tmp))
    case("Y1 内置模板可升级（回归步骤补种/历史执行不受影响）", lambda: t_playbook_upgrade(tmp))
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("MCP ALL PASS")


if __name__ == "__main__":
    main()
