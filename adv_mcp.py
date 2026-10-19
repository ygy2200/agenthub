# -*- coding: utf-8 -*-
"""AgentHub MCP server 对抗回归：协议层 + 端到端子进程。

运行：python adv_mcp.py
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

import contextlib

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
    assert r["serverInfo"]["version"] == "2.40.58"
    assert m.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, str(root)) is None
    tools = resp_ok(m.handle_message(rpc("tools/list"), str(root)))["tools"]
    names = {t["name"] for t in tools}
    assert {"hub_list_projects", "hub_log_work", "hub_memory_read", "hub_memory_write",
            "hub_get_progress", "hub_list_skills", "hub_list_mcps", "hub_search",
            "hub_get_project", "hub_create_project", "hub_get_rules", "hub_heartbeat",
            "hub_report_error", "hub_list_errors", "hub_error_close", "hub_undo", "hub_list_agents",
            "hub_list_todos", "hub_health", "hub_todo_done", "hub_env_set", "hub_env_list",
            "hub_search_files", "hub_distill", "hub_get_record", "hub_duplicates", "hub_report",
            "hub_handoff", "hub_status", "hub_user_profile", "hub_note", "hub_note_read",
            "hub_ask", "hub_metacognition",
            "hub_help", "hub_search_tools", "hub_call_tool", "hub_audit", "hub_flush_index",
            "hub_task_add", "hub_task_update", "hub_claim", "hub_tasks",
            "hub_next", "hub_crystallize",
            "hub_event_write", "hub_events",
            "hub_skill_match", "hub_skill_sync"} <= names, names
    assert len(names) == 69  # 68=X3+X4+Y1+Y2+Y3+playbook/trigger/graph；69=+hub_error_close（2026-10-17 错误闭环：MCP 侧关错，治僵尸堆积）
    # 未知方法
    msg = m.handle_message(rpc("no/such"), str(root))
    assert msg["error"]["code"] == -32601
    # limit 非法值容错（v1.3.1：不抛异常；v2.40.14 起契约收紧为「清晰整数错误」——
    # 仍是「不崩不断连」，但错误必须能被模型纠正，不再悄悄回落默认值让模型以为生效）
    r = m.handle_message(rpc("tools/call", {"name": "hub_get_progress", "arguments": {"limit": "abc"}}, 2), str(root))
    assert r["result"]["isError"] is True and "必须是整数" in r["result"]["content"][0]["text"], r
    # 工具抛异常 -> isError 且自动落盘错误登记（mock 掉 call_tool 模拟任意异常）
    from unittest import mock
    with mock.patch.object(m, "call_tool", side_effect=RuntimeError("模拟崩溃")):
        r = m.handle_message(rpc("tools/call", {"name": "hub_x", "arguments": {"agent": "zcode"}}, 3), str(root))
    assert r["result"]["isError"] is True and "RuntimeError" in r["result"]["content"][0]["text"]
    assert "模拟崩溃" not in r["result"]["content"][0]["text"]  # 原始异常消息不得回吐模型（wati.io）
    errs = brain.error_list(str(root))
    assert any("hub_x" in e.get("title", "") and e.get("agent") == "zcode" for e in errs), \
        "工具异常未自动登记错误"
    # ping
    assert "result" in m.handle_message(rpc("ping"), str(root))


def t_protocol_encoding(root):
    """Round 8：协议帧编码加固回归（bigiron.cc: Python MCP on Windows UTF-8 陷阱）。

    纯中文工具在 Windows 上若 stdout 非 UTF-8（reconfigure 失败回落 cp1252），
    json.dumps(ensure_ascii=False) 的中文写入会抛 UnicodeEncodeError → 协议帧写不出 → 客户端 hang。
    R8 把协议帧改为 ensure_ascii=True（恒纯 ASCII），跨编码绝不会炸；客户端 json.loads 自动还原中文。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    assert "json.dumps(resp, ensure_ascii=True)" in src, \
        "R8 契约：协议写出必须用 ensure_ascii=True，杜绝非 UTF-8 stdout 下的中文写入崩溃"
    # 端到端：中文响应经纯 ASCII 帧往返无损（hub_status 不依赖 agent 身份，测试环境可用）
    r = resp_ok(m.handle_message(rpc("tools/call", {"name": "hub_status", "arguments": {}}), str(root)))
    assert r["isError"] is False, f"状态工具应成功，实际 {r}"
    text = r["content"][0]["text"]
    frame = json.dumps({"result": r}, ensure_ascii=True)
    frame.encode("ascii")  # 纯 ASCII 帧：cp1252 等非 UTF-8 编码也绝不抛 UnicodeEncodeError
    assert json.loads(frame)["result"]["content"][0]["text"] == text, "中文经 ASCII 帧往返必须无损"
    # tools/list 中文描述同样纯 ASCII 帧往返无损
    tools = resp_ok(m.handle_message(rpc("tools/list"), str(root)))["tools"]
    desc_frame = json.dumps(tools, ensure_ascii=True)
    desc_frame.encode("ascii")
    assert any(("搜索" in t["description"] or "记录" in t["description"]) for t in tools), \
        "工具描述中文必须随 ASCII 帧无损传输"


def t_round10_checkpoint_no_tid(root):
    """Round 10：hub_checkpoint 的 list/latest 无 task_id 不再退化成「任务 #None」。

    此前 brain.checkpoint_list/latest 对 None 调 int(None) 抛异常返回空/None，
    工具层却渲染成「任务 #None 还没有检查点」——把「漏传参数」伪装成「数据问题」，
    且 #None 不专业。现在无 task_id 直接给清晰必填提示（isError=True，模型可自我纠正）。
    """
    def call(action, tid=None):
        args = {"action": action}
        if tid is not None:
            args["task_id"] = tid
        r = m.handle_message(rpc("tools/call", {"name": "hub_checkpoint", "arguments": args}), str(root))
        return r["result"]

    for action in ("list", "latest"):
        res = call(action)
        assert res["isError"] is True, (action, res)
        assert "task_id" in res["content"][0]["text"], (action, res)
        assert "#None" not in res["content"][0]["text"], (action, res)
    # 有 task_id（不存在的任务）→ 不再退化 #None，正常提示无检查点且无错误
    res = call("list", 999999)
    assert res["isError"] is False, res
    assert "#None" not in res["content"][0]["text"], res


def t_round11_int_param_type(root):
    """Round 11：整数入参传坏类型不再伪装成「服务端故障/联系管理员」。

    此前 hub_graph（neighbors/related/path/isolated/clusters）与 hub_playbook（runs）直接
    `int(arguments.get(...))`——模型传 "abc" 抛 ValueError，被通用兜底渲染成
    「工具执行失败（ValueError）…联系管理员」：入参问题伪装成服务端故障（模型无法自我纠正），
    且错误登记归类成「执行异常」淹没真故障。现在统一给「错误：参数 X 必须是整数」。
    """
    def res_of(name, args):
        r = m.handle_message(rpc("tools/call", {"name": name, "arguments": args}), str(root))
        return r["result"]

    # ① 5 个 hub_graph 动作 + hub_playbook：坏类型 → 清晰整数提示（isError=True）
    bad_calls = [
        ("hub_graph", {"action": "clusters", "limit": "abc"}),
        ("hub_graph", {"action": "isolated", "limit": "x"}),
        ("hub_graph", {"action": "neighbors", "node": "record:1", "limit": "bad"}),
        ("hub_graph", {"action": "related", "node": "record:1", "limit": "2.5"}),
        ("hub_graph", {"action": "path", "from_node": "a", "to_node": "b", "max_depth": "nan"}),
        ("hub_playbook", {"action": "runs", "limit": "oops"}),
    ]
    for name, args in bad_calls:
        res = res_of(name, args)
        body = res["content"][0]["text"]
        assert res["isError"] is True, (name, args, res)
        assert "必须是整数" in body, (name, args, body)
        assert "联系管理员" not in body and "工具执行失败" not in body, (name, args, body)

    # ② 合法值不受影响：数字串 / 整数 / 缺省（零回归）
    for lim in ("5", 5, None):
        args = {"action": "clusters"}
        if lim is not None:
            args["limit"] = lim
        res = res_of("hub_graph", args)
        assert res["isError"] is False, (lim, res)
        assert "必须是整数" not in res["content"][0]["text"], (lim, res)
    res = res_of("hub_playbook", {"action": "runs", "limit": "3"})
    assert res["isError"] is False, res

    # ③ 通用兜底是「安全网」：逃逸的 Type/ValueError 归类为入参问题（不再渲染成服务端故障）。
    #    注意：②的 _as_int 走的是「正常返回清晰错误」，与 R6 必填校验一致——不登记错误库；
    #    只有逃逸到通用兜底的异常才登记，且这里要归到「入参类型错误」而非「执行异常」。
    orig = m.call_tool

    def _boom_value(*a, **k):
        raise ValueError("invalid literal for int() with base 10: 'abc'")
    try:
        m.call_tool = _boom_value
        res = res_of("hub_status", {})
        body = res["content"][0]["text"]
        assert res["isError"] is True, res
        assert "入参不符合要求" in body, body
        assert "联系管理员" not in body and "工具执行失败" not in body, body
    finally:
        m.call_tool = orig
    errs = brain.error_list(str(root), limit=100)
    titles = [e.get("title", "") for e in errs]
    assert any("入参类型错误" in t for t in titles), titles


def t_round12_date_normalize(root):
    """Round 12：日期入参规范化——修「格式偏差 → 到期提醒静默失效」。

    M11 决策回顾队列 / 任务超期 / 记忆有效期都走 SQLite `<= date('now')` 的**字典序**比较：
    '2026/10/01'（斜杠）在索引 4 是 '/'（0x2F）> '-'（0x2D），恒大于同日期的 ISO 串——
    也就是「明明是过去的日期，却永远不进到期队列、永远不提醒」，且无任何报错。
    """
    def res_of(name, args):
        r = m.handle_message(rpc("tools/call", {"name": name, "arguments": args}), str(root))
        return r["result"]

    today = datetime.date.today()
    slash_this_month = f"{today.year}/{today.month:02d}/01"      # 斜杠格式、≤ 今天
    expect_iso = f"{today.year}-{today.month:02d}-01"

    # ① 不可解析 / 非法日期 → 清晰错误（模型可自我纠正）
    bad = [
        ("hub_log_work", {"project": "测试-项目", "agent": "zcode", "content": "决策：x",
                          "decision": "x", "review_date": "tomorrow"}),
        ("hub_task_add", {"title": "任务X", "agent": "zcode", "due": "下周五"}),
        ("hub_memory_write", {"content": "记忆X", "agent": "zcode", "valid_until": "2026-02-30"}),
    ]
    for name, args in bad:
        res = res_of(name, args)
        body = res["content"][0]["text"]
        assert res["isError"] is True, (name, args, res)
        assert "YYYY-MM-DD" in body or "合法日期" in body, (name, body)

    # ② 斜杠格式「已到期」决策：规范成 ISO 后真的进到期队列（修复的核心证据）。
    #    修复前该日期字典序更大 → 永不触发（静默失效）。
    res = res_of("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                  "content": "决策：斜杠日期验证", "decision": "选 A",
                                  "review_date": slash_this_month})
    assert res["isError"] is False, res
    due = brain.decision_review_due(str(root), 200)
    assert any(d.get("review_date") == expect_iso for d in due), \
        f"斜杠日期未规范成 {expect_iso} 并进到期队列：{[(d.get('record_id'), d.get('review_date')) for d in due]}"

    # ③ 任务 due 同样规范化入库
    res = res_of("hub_task_add", {"title": "斜杠截止任务", "agent": "zcode",
                                  "project": "测试-项目", "due": slash_this_month})
    assert res["isError"] is False, res
    rows = [t for t in brain.task_list(str(root), limit=200)
            if t["title"] == "斜杠截止任务"]
    assert rows and rows[0]["due"] == expect_iso, rows

    # ④ 缺省 / 空值零回归（不传行为与旧版一致）
    assert res_of("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                   "content": "无回顾日决策"})["isError"] is False
    assert res_of("hub_task_add", {"title": "无截止任务", "agent": "zcode"})["isError"] is False
    assert res_of("hub_memory_write", {"content": "无有效期记忆", "agent": "zcode"})["isError"] is False


def t_round13_int_params_central(root):
    """Round 13：18 个工具的整数入参坏类型不再静默回落默认值（集中式声明 + 漂移守卫）。

    此前这些字段是 `try/except → 默认值`：模型传 "abc" 时参数被**悄悄忽略**
    （如 supersedes 传错 → 旧记忆根本没被取代，模型以为生效了）。现在集中在 call_tool
    入口按 `_INT_PARAMS` 声明校验，坏类型给「错误：参数 X 必须是整数」。
    """
    def res_of(name, args):
        r = m.handle_message(rpc("tools/call", {"name": name, "arguments": args}), str(root))
        return r["result"]

    # ① 漂移守卫：源码里每个 `int(arguments.get("<k>"` 直转点，其 (工具, 参数) 必须已声明或豁免
    src = Path(m.__file__).read_text(encoding="utf-8").splitlines()
    declared = {(t, k) for t, ks in m._INT_PARAMS.items() for k in ks}
    cur = None
    undeclared = []
    for i, ln in enumerate(src, 1):
        mm = re.match(r'\s*if name == "(\w+)"', ln)
        if mm:
            cur = mm.group(1)
        m3 = re.search(r'int\(arguments\.get\(\s*"(\w+)"', ln)
        if m3 and m3.group(1) not in m._INT_EXEMPT_PARAMS and (cur, m3.group(1)) not in declared:
            undeclared.append((i, cur, m3.group(1)))
    assert not undeclared, f"仍有未声明/未豁免的整数直转点（请登记 _INT_PARAMS）：{undeclared}"

    # ② 机制单测：声明字段坏类型拦、豁免字段放行、缺省/空值放行
    assert m._bad_int_param("hub_events", {"since_id": "abc"}) and "since_id" in \
        m._bad_int_param("hub_events", {"since_id": "abc"})
    assert m._bad_int_param("hub_memory_write", {"supersedes": "x"}) is not None
    assert m._bad_int_param("hub_tasks", {"limit": None}) is None
    assert m._bad_int_param("hub_tasks", {"limit": ""}) is None
    assert m._bad_int_param("hub_tasks", {}) is None
    assert m._bad_int_param("hub_tasks", {"limit": "5"}) is None       # 数字串合法
    assert m._bad_int_param("hub_status", {"max_chars": "abc"}) is None   # 豁免：协议级预算
    assert m._bad_int_param("hub_note_read", {"note_id": "abc"}) is None  # 豁免：自身已清晰

    # ③ 端到端：坏类型 → 清晰整数错误（不再静默回落）
    bad_calls = [
        ("hub_tasks", {"limit": "abc"}),
        ("hub_next", {"limit": "abc"}),
        ("hub_list_todos", {"limit": "abc"}),
        ("hub_get_record", {"record_id": "abc"}),
        ("hub_memory_read", {"limit": "abc"}),
        ("hub_events", {"since_id": "abc"}),
        ("hub_audit", {"limit": "abc"}),
        ("hub_export", {"action": "export", "limit": "abc"}),
    ]
    for name, args in bad_calls:
        res = res_of(name, args)
        body = res["content"][0]["text"]
        assert res["isError"] is True, (name, args, res)
        assert "必须是整数" in body, (name, args, body)

    # ④ 合法值零回归（数字串 / 整数 / 缺省均照常）
    for lim in ("5", 5, None):
        args = {"action": "list"}
        if lim is not None:
            args["limit"] = lim
        res = res_of("hub_audit", args)
        assert res["isError"] is False, (lim, res)
        assert "必须是整数" not in res["content"][0]["text"], (lim, res)

    # ⑤ 关键语义：supersedes 传坏类型必须报错，绝不能静默写出一条「没取代」的记忆
    res = res_of("hub_memory_write", {"content": "R13 supersedes 坏类型验证", "supersedes": "abc"})
    assert res["isError"] is True and "必须是整数" in res["content"][0]["text"], res


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
    # hub_error_close（2026-10-17）：MCP 侧关错闭环——修完必须关，否则僵尸堆积
    out = m.call_tool("hub_report_error", {"agent": "x", "title": "待关闭的错误"}, root)
    assert "已登记" in out
    cid = max(e["id"] for e in brain.error_list(root))
    out = m.call_tool("hub_error_close", {"error_id": "abc"}, root)
    assert "必须是整数" in out, out
    out = m.call_tool("hub_error_close", {"error_id": -1}, root)
    assert "正整数" in out, out
    out = m.call_tool("hub_error_close", {"error_id": 999999}, root)
    assert "未找到" in out, out
    out = m.call_tool("hub_error_close", {"error_id": cid, "agent": "zcode", "note": "已修"}, root)
    assert "已关闭错误" in out and f"#{cid}" in out, out
    out = m.call_tool("hub_error_close", {"error_id": cid}, root)
    assert "已是关闭状态" in out, out
    out = m.call_tool("hub_list_errors", {"status": "open"}, root)
    assert "待关闭的错误" not in out
    out = m.call_tool("hub_list_errors", {"status": "fixed"}, root)
    assert "待关闭的错误" in out


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
    assert "必须是整数" in out, out   # v2.40.14：坏类型给清晰错误（原为静默回落默认值）
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
    # 畸形 id 容错：整数类畸形（0/-1/None/越界）→「未找到记录」；
    # 非整数（"abc"）v2.40.14 起给清晰整数错误——原先把 "abc" 当 0 报「未找到记录 #0」，
    # 属于「把参数问题伪装成数据问题」（模型会一直换个 id 重试）。
    for bad in (0, -1, None, 999999):
        out = m.call_tool("hub_get_record", {"record_id": bad}, root_s)
        assert "未找到记录" in out, (bad, out)
    out = m.call_tool("hub_get_record", {"record_id": "abc"}, root_s)
    assert "必须是整数" in out, out
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
    # 空参数不崩：question 缺失 → 集中校验返回清晰的必填提示（isError=True），而非崩溃
    empty = m.call_tool("hub_ask", {}, rs)
    assert "必填参数" in empty and "question" in empty, empty


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


def t_iserror_correctness(root):
    """isError 正确性（2026-10-13 缺陷自查补）：业务失败必须以 isError=True 返回，
    否则 MCP 客户端把失败当成功、模型无法自我纠正（MCP 官方反对的反模式）。

    覆盖：① 成功读工具 isError=False；② 缺必填参数（hub_task_add 无 title）isError=True；
    ③ 未知工具经 hub_call_tool 转发 isError=True；④ hub_claim 缺 agent 的未加前缀错误
    也必须是 isError=True（验证 L272 归一化）。
    """
    def call(name, args):
        r = m.handle_message(rpc("tools/call", {"name": name, "arguments": args}), str(root))
        return r["result"]

    ok = call("hub_status", {})
    assert ok["isError"] is False, f"成功读工具应为 isError=False，实际 {ok}"

    miss = call("hub_task_add", {})
    assert miss["isError"] is True, f"缺必填参数应为 isError=True，实际 {miss}"
    assert "错误" in miss["content"][0]["text"], f"错误文案应含「错误」，实际 {miss}"

    unk = call("hub_call_tool", {"name": "hub_no_such_tool_xyz"})
    assert unk["isError"] is True, f"未知工具转发应为 isError=True，实际 {unk}"

    # 直接 tools/call 一个不存在的工具名（不经 hub_call_tool）也必须 isError=True
    direct_unk = call("hub_no_such_tool_direct", {})
    assert direct_unk["isError"] is True, f"直接调用未知工具应为 isError=True，实际 {direct_unk}"

    claim = call("hub_claim", {"task_id": 1})
    assert claim["isError"] is True, f"hub_claim 缺 agent 应为 isError=True，实际 {claim}"


def t_response_size_guard():
    """响应体积双封顶（2026-10-13 缺陷自查补）：防 stdio 管道死锁 / 上下文冲爆。
    ① 默认 4000 预算：超大文本截断 + full_text 内联 ≤16K；② max_chars=0 下失控输出(>64K)兜底截断。"""
    big = "x" * 200_000
    # 默认预算
    text, structured = m._shape_result(big, {})
    assert len(text) <= 4000 + 200, f"默认路径文本应≤预算，实际 {len(text)}"
    assert structured is not None and len(structured["full_text"]) <= m.STRUCTURED_FULL_CAP + 200
    assert "已截断" in text and "未全量内联" in text, "超大结果应提示截断 + full_text 未全量内联"
    # max_chars=0 但失控输出
    text0, _s0 = m._shape_result(big, {"max_chars": 0})
    assert len(text0) <= m.HARD_MAX_CHARS + 200, f"max_chars=0 失控输出应兜底，实际 {len(text0)}"
    assert "响应硬上限" in text0, "应明确告知触发硬上限"
    # 正常小文本不受影响
    small = "ok"
    t1, s1 = m._shape_result(small, {})
    assert t1 == "ok" and s1 is None
    t2, s2 = m._shape_result(small, {"max_chars": 0})
    assert t2 == "ok" and s2 is None


def t_handle_message_robustness(root):
    """协议健壮性（2026-10-13 缺陷自查补）：① 畸形请求（非 dict）回 -32600 而非崩服务；
    ② 工具抛异常时模型侧文案不得回吐原始路径/SQL（细节只留服务端）。来源：arxiv 2606.05339
    （请求处理前未校验是高发故障）+ wati.io（禁止把堆栈/SQL 塞回模型）。"""
    # ① 畸形请求 / 缺 method
    bad = m.handle_message(["not", "a", "dict"], str(root))
    assert bad["error"]["code"] == -32600, bad
    miss = m.handle_message({"jsonrpc": "2.0", "method": "no/such/method", "id": 1}, str(root))
    assert miss["error"]["code"] == -32601, miss

    # ② 异常不外泄原始细节
    orig = m.call_tool

    def _boom(*a, **k):
        raise RuntimeError("boom at C:\\secret\\db.sqlite3 : UNIQUE constraint failed records.x")
    try:
        m.call_tool = _boom
        r = m.handle_message(rpc("tools/call", {"name": "hub_status", "arguments": {}}), str(root))
        res = r["result"]
        assert res["isError"] is True, res
        body = res["content"][0]["text"]
        assert "C:\\secret" not in body, f"原始路径不应回吐模型：{body}"
        assert "UNIQUE constraint" not in body, f"原始 SQL 不应回吐模型：{body}"
        assert "工具执行失败" in body and "RuntimeError" in body, f"应保留异常类型：{body}"
    finally:
        m.call_tool = orig


def t_required_validation(root):
    """集中式必填校验（Round 6 / 2026-10-13）：执行前按 TOOLS 声明的 required 拦缺失参数。
    覆盖：① 直接 tools/call 缺 project → isError=True 且提示缺失参数；② hub_search 缺 keyword
    同样拦截；③ 带必填参数 → 不触发拦截（走正常路径）；④ agent 豁免：hub_heartbeat 不传
    agent 不应被「必填参数 缺失」拦截；⑤ hub_checkpoint 整工具豁免；⑥ hub_call_tool 透传
    也会对 inner 工具校验（统一覆盖）。机制单测：_required_missing 直接断言豁免与判定逻辑。"""
    def call(name, args):
        r = m.handle_message(rpc("tools/call", {"name": name, "arguments": args}), str(root))
        return r["result"]

    # ① 缺 project（文案形如「错误：必填参数 project 缺失（...）」）
    miss = call("hub_get_project", {})
    assert miss["isError"] is True, miss
    assert "必填参数" in miss["content"][0]["text"] and "缺失" in miss["content"][0]["text"] \
        and "project" in miss["content"][0]["text"]

    # ② 缺 keyword
    miss2 = call("hub_search", {})
    assert miss2["isError"] is True and "keyword" in miss2["content"][0]["text"], miss2

    # ③ 带必填参数 → 不触发集中拦截（测试 hub 内「测试-项目」存在，应成功）
    ok = call("hub_get_project", {"project": "测试-项目"})
    assert ok["isError"] is False, ok

    # ④ agent 豁免：hub_heartbeat 无 agent 不应出现集中校验的「必填参数」提示
    hb = call("hub_heartbeat", {})
    assert "必填参数" not in hb["content"][0]["text"], \
        f"agent 应豁免集中校验：{hb['content'][0]['text']}"

    # ⑤ hub_checkpoint 整工具豁免：无 task_id 不出现集中校验的「必填参数」提示
    cp = call("hub_checkpoint", {"action": "list"})
    assert "必填参数" not in cp["content"][0]["text"], \
        f"checkpoint 应豁免：{cp['content'][0]['text']}"

    # ⑥ hub_call_tool 透传：inner 缺必填也拦
    via = call("hub_call_tool", {"name": "hub_get_project", "arguments": {}})
    assert via["isError"] is True and "project" in via["content"][0]["text"], via

    # 机制单测：豁免与判定
    assert m._required_missing("hub_heartbeat", {}) == [], "agent 应豁免"
    assert m._required_missing("hub_log_work", {"project": "p", "content": "c"}) == [], \
        "agent 豁免后无缺失"
    assert m._required_missing("hub_log_work", {}) == ["project", "content"], \
        m._required_missing("hub_log_work", {})
    assert m._required_missing("hub_checkpoint", {}) == [], "checkpoint 整工具豁免"
    assert m._required_missing("hub_get_project", {}) == ["project"]
    assert m._effective_required("hub_heartbeat") == [], "heartbeat 有效必填空（agent 剔除）"
    assert m._effective_required("hub_log_work") == ["project", "content"]


def t_round7_iserror_notfound(root):
    """Round 7（2026-10-13）：业务失败返回路径必须带「错误：」前缀，否则被客户端当成功
    （isError 正确性 Round 1 的收口项）。覆盖 3 处原本缺前缀、静默成功的路径：
    hub_get_project 项目不存在 / hub_history 条目不存在 / hub_playbook show 流程不存在。"""
    def call(name, args):
        r = m.handle_message(rpc("tools/call", {"name": name, "arguments": args}), str(root))
        return r["result"]

    miss_proj = call("hub_get_project", {"project": "不存在的项目XYZ"})
    assert miss_proj["isError"] is True, miss_proj
    assert "不存在" in miss_proj["content"][0]["text"], miss_proj

    miss_hist = call("hub_history", {"id": 999999})
    assert miss_hist["isError"] is True, miss_hist
    assert "不存在" in miss_hist["content"][0]["text"], miss_hist

    miss_pb = call("hub_playbook", {"action": "show", "name": "不存在的流程XYZ"})
    assert miss_pb["isError"] is True, miss_pb
    assert "不存在" in miss_pb["content"][0]["text"], miss_pb


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

    # 检索接入图路（Graphiti 同款第四路）：与查询词零重叠但图上相连的条目应被召回
    brain.add_memory(rs, "锚点记忆：只在它里面有 zzzmarker 这个词", tags="图检索",
                     project="测试-图谱", agent="zcode")
    brain.add_memory(rs, "另一条完全不提那个标记的记忆", tags="图检索",
                     project="测试-图谱", agent="zcode")
    brain.add_memory(rs, "八竿子打不着的无关记忆", tags="别的标签",
                     project="别的-项目", agent="zcode")
    brain.graph_build(rs)
    hits = brain.search_memories(rs, "zzzmarker", limit=10)
    texts = [h["content"] for h in hits]
    assert any("zzzmarker" in t for t in texts), f"锚点自身未命中：{texts}"
    assert any("完全不提" in t for t in texts), f"图路未召回零重叠条目：{texts}"
    assert any("graph" in (h.get("_channels") or {}) for h in hits), "命中应带 graph 通道标记"
    assert not any("八竿子" in t for t in texts), f"无关记忆被误召回：{texts}"
    # 近重复折叠（业界流水线第 3 步：进上下文前 dedup）
    brain.add_memory(rs, "折叠测试：SQLite 备份必须走 backup API", tags="折叠",
                     project="测试-图谱", agent="zcode")
    brain.add_memory(rs, "折叠测试：SQLite 备份必须走 backup API！", tags="折叠",
                     project="测试-图谱", agent="zcode")
    hits2 = brain.search_memories(rs, "SQLite 备份 backup API", limit=10, count_hits=False)
    n_dup = sum(1 for h in hits2 if "折叠测试" in (h.get("content") or ""))
    assert n_dup == 1, f"近重复应折叠成 1 条，实际 {n_dup}：{[h['content'][:40] for h in hits2]}"
    assert any(int(h.get("_dup_n") or 0) >= 1 for h in hits2), "折叠应留痕 _dup_n"

    # 矛盾检测（业界：取回后、进上下文前检测互斥，别让 LLM 自己发现）
    # 形态 = 新条包含旧条全文 + 修正词（"一条修正另一条"的典型写法）
    brain.add_memory(rs, "矛盾测试：AgentHub 部署目录是 ~/.agenthub/mcp_server", tags="矛盾",
                     project="测试-图谱", agent="zcode")
    brain.add_memory(rs, "矛盾测试：AgentHub 部署目录是 ~/.agenthub/mcp_server——不对，实际是 ~/.agenthub/server",
                     tags="矛盾", project="测试-图谱", agent="zcode")
    hits3 = brain.search_memories(rs, "AgentHub 部署目录", limit=10, count_hits=False)
    marked = [h for h in hits3 if h.get("_conflict_with")]
    assert len(marked) == 2, f"应两条互标，实际 {len(marked)}：{[m['content'][:30] for m in marked]}"
    out3 = m.call_tool("hub_search", {"keyword": "AgentHub 部署目录"}, rs)
    assert "疑似互斥" in out3, f"hub_search 未给可行动提示：{out3[-200:]}"
    # 无修正词的相似条（只相似不互斥）不该被标
    assert not any(h.get("_conflict_with") for h in hits3
                   if "SQLite 备份" in (h.get("content") or "")), "非互斥不应被标记"

    # 跨类型图增强（FalkorDB：从语义发现出发，沿图做结构增强）
    r1c = brain.add_record(rs, "跨类-项目", "zcode", "2026-10-13", "备份方案设计记录",
                           "这份记录讲备份方案 zzzrecord")
    m1c = brain.add_memory(rs, "完全不同的记忆内容 zzzmemory", tags="跨类",
                           project="跨类-项目", agent="zcode")
    brain.graph_build(rs)
    allr = brain.search_all(rs, "备份方案")
    rec_ids = [x["id"] for x in allr["records"]]
    mem_ids = [x["id"] for x in allr["memories"]]
    assert r1c in rec_ids, "同项目记录应命中"
    assert m1c in mem_ids, f"跨类型记忆未被带回：{mem_ids}"
    assert any(x.get("_cross") for x in allr["memories"]), "跨类型候选应带 _cross 标记"
    # limit 必须真生效（2026-10-13 抓到的真 bug：search_all 位置参把 limit 传进了 kind）
    lim = brain.search_all(rs, "备份方案", limit=3)
    assert len(lim["memories"]) <= 3 + 8 and len(lim["records"]) <= 3 + 8, \
        f"search_all 的 limit 未生效：{len(lim['records'])}/{len(lim['memories'])}"
    # 不破坏单类型检索契约（search_records 只回记录）
    assert all("title" in x for x in brain.search_records(rs, "备份方案", limit=5))
    # 可解释性（铁律 3）：图关联条目要在输出里说明来源，否则 agent 不知道它为什么出现
    out_all = m.call_tool("hub_search", {"keyword": "备份方案"}, rs)
    assert "图关联" in out_all, f"hub_search 未标注图关联来源：{out_all[:400]}"
    # hub_health 报库级疑似互斥对（互斥= agent 可能拿矛盾事实干活，比重复更危险）
    hp = brain.health_report(rs)
    assert "conflict_pair_count" in hp and hp["conflict_pair_count"] >= 1, "health 未报互斥对"
    assert "疑似互斥" in m.call_tool("hub_health", {}, rs)
    # records 也要做矛盾标记（与 memories 对称，2026-10-13 补）
    brain.add_record(rs, "测试-图谱", "zcode", "2026-10-13", "记录矛盾甲",
                     "本项目备份目录是 _hub/backup")
    brain.add_record(rs, "测试-图谱", "zcode", "2026-10-13", "记录矛盾乙",
                     "本项目备份目录是 _hub/backup——不对，实际是 _hub/backups")
    rc = brain.search_records(rs, "本项目备份目录", limit=10)
    assert any(r.get("_conflict_with") for r in rc), f"记录矛盾未标记：{[r['title'] for r in rc]}"
    # 孤岛给补标签建议（别再让人从零想该打什么标签）
    brain.add_memory(rs, "有标签的记忆甲", tags="建议标签测试", project="孤岛-项目", agent="zcode")
    iso_mem = brain.add_memory(rs, "没有标签的孤岛记忆", tags="", project="孤岛-项目", agent="zcode")
    brain.graph_build(rs)
    iso2 = brain.graph_isolated(rs, limit=50)
    row = next((x for x in iso2 if x["node"] == f"memory:{iso_mem}"), None)
    assert row, "无标签记忆应进孤岛清单（口径：记忆看标签边）"
    assert "建议标签测试" in (row.get("suggested_tags") or []), f"未给建议标签：{row}"
    # stats 与清单同口径：无标签记忆计入孤岛
    st3 = brain.graph_stats(rs, build=False)
    assert st3["isolated"] >= 1, f"stats 孤岛数应含无标签记忆：{st3}"

    # 图路可关闭（独立降级开关）
    brain._RRF_W_GRAPH = 0.0
    try:
        off = [h["content"] for h in brain.search_memories(rs, "zzzmarker", limit=10)]
        assert not any("完全不提" in t for t in off), f"关闭图路后不该再召回：{off}"
    finally:
        brain._RRF_W_GRAPH = 1.0


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


def t_error_msg_three_part(root):
    """R3·错误信息三件套（什么错了/期望什么/正确示例）——外部最佳实践落地验证。

    依据：tsecurity.de / agentpatterns.io「Error messages are recovery instructions」——
    错误必须能被模型自我纠正，不含示例的错误浪费整轮对话。注入坏输入，断言每条
    错误都带可行动信息（含示例或核对路径）。
    """
    root = str(root)
    # ① 必填缺失 → 自动带该工具描述里的示例（同源不漂移）
    out = m.call_tool("hub_log_work", {"agent": "zcode"}, root)
    assert "缺失" in out and "project" in out and "正确示例" in out, out
    # ② 项目不存在 → 相近项目 + 核对方式 + 示例
    out = m.call_tool("hub_get_project", {"project": "不存在的项目-XYZ"}, root)
    assert "不存在" in out and "hub_list_projects" in out and "例：" in out, out
    # ③ record_type 非法 → 合法值 + 示例（该校验先于项目存在性，顺序稳定）
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                       "content": "x", "record_type": "bogus"}, root)
    assert "只能是" in out and "work_log" in out and "例：" in out, out
    # ④ 未知工具 → 搜索指引 + 具体搜索示例
    out = m.call_tool("hub_no_such_tool", {}, root)
    assert "未知工具" in out and "hub_search_tools" in out and "keyword=" in out, out
    # ⑤ 根目录不存在 → 排查指引（config.json / --root）
    out = m.call_tool("hub_health", {}, "/no/such/dir")
    assert "根目录不存在" in out and "config.json" in out, out
    # ⑥ 随记 content 缺失 → 集中必填校验先于分发，断言可行动信息（缺失+示例）
    out = m.call_tool("hub_note", {"tags": "x"}, root)
    assert "content" in out and "缺失" in out and "例：" in out, out


def t_template_nudge(root):
    """R7·四段式模板软提示（2026-10-17）：缺段只在回显点一句，不阻断、不污染存储。

    背景：铁律四段式实测遵守率仅 48%（近 200 条）——标准没有度量与提示必然腐烂。
    """
    root = str(root)
    out = m.call_tool("hub_log_work", {"project": "模板测试-项目", "agent": "zcode",
                                       "content": "目的：x\n做了什么：y\n验证结果：z\n如何回滚：w"}, root)
    assert "模板提示" not in out, out   # 四段俱全不提示
    out = m.call_tool("hub_log_work", {"project": "模板测试-项目", "agent": "zcode",
                                       "content": "随手记了一条"}, root)
    assert "模板提示" in out and "验证结果" in out and "如何回滚" in out, out
    # 存储内容不被污染（提示只进回显，不进 records.content）
    rows = brain.search_records(root, "随手记了一条")
    assert rows and "模板提示" not in (rows[0].get("content") or ""), rows[0]["content"][:80]
    # todo 类型不适用四段式，不提示
    out = m.call_tool("hub_log_work", {"project": "模板测试-项目", "agent": "zcode",
                                       "content": "待办：后续验证", "record_type": "todo"}, root)
    assert "模板提示" not in out, out


def t_confirm_blast_radius(root):
    """B3 回归：确认闸门破坏半径分级（HIGH_BLAST_TOOLS + 严格模式开关）。
    默认保持「不传 confirm 即执行」的历史语义——实测回归里高危工具 60+ 处调用全都不带
    confirm，直接翻转默认会打断 6 个接入 agent 的日常写入，故做成可开关而非硬改默认。
    验证：①分级集合是 WRITE_TOOLS 子集（不凭空造名）；②默认模式下高危不传 confirm 照常
    执行（防回归）；③严格模式下高危不传 confirm → input_required；④显式 confirm=true
    放行；⑤严格模式下低危写入（hub_note）不受影响（否则每次心跳都被卡死）。"""
    assert m.HIGH_BLAST_TOOLS <= m.WRITE_TOOLS, m.HIGH_BLAST_TOOLS - m.WRITE_TOOLS
    saved = os.environ.get("AGENTHUB_STRICT_CONFIRM")
    try:
        os.environ.pop("AGENTHUB_STRICT_CONFIRM", None)
        r = resp_ok(m.handle_message(
            rpc("tools/call", {"name": "hub_flush_index", "arguments": {}}), str(root)))
        assert r.get("resultType") != "input_required", "默认模式下高危工具不应被拦（会打断现有 agent）"
        os.environ["AGENTHUB_STRICT_CONFIRM"] = "1"
        r2 = resp_ok(m.handle_message(
            rpc("tools/call", {"name": "hub_flush_index", "arguments": {}}), str(root)))
        assert r2.get("resultType") == "input_required", r2
        assert "高破坏半径" in r2["content"][0]["text"], r2
        r3 = resp_ok(m.handle_message(
            rpc("tools/call", {"name": "hub_flush_index", "arguments": {"confirm": True}}), str(root)))
        assert r3.get("resultType") != "input_required", r3
        assert "hub_note" not in m.HIGH_BLAST_TOOLS
        r4 = resp_ok(m.handle_message(
            rpc("tools/call", {"name": "hub_note", "arguments": {"content": "B3 分级测试笔记"}}), str(root)))
        assert r4.get("resultType") != "input_required", r4
    finally:
        if saved is None:
            os.environ.pop("AGENTHUB_STRICT_CONFIRM", None)
        else:
            os.environ["AGENTHUB_STRICT_CONFIRM"] = saved


def t_structured_log(root):
    """B8 回归：结构化可观测（AGENTHUB_LOG → 每次工具调用落一行 JSONL）。
    验证：①默认关闭零副作用（未设环境变量绝不落文件，否则常驻 server 会被拖慢/撑爆磁盘）
    ②开启后字段齐全（ts/tool/ms/ok/err_kind/agent/root）③异常路径也落痕（err_kind 非空）
    ——观测不到服务端异常的可观测性是假的 ④日志写失败不得拖垮主链路（观测 = 旁路）。"""
    log = Path(root) / "mcp_events.jsonl"
    saved = os.environ.get("AGENTHUB_LOG")
    try:
        os.environ.pop("AGENTHUB_LOG", None)
        resp_ok(m.handle_message(rpc("tools/call", {"name": "hub_health", "arguments": {}}), str(root)))
        assert not log.exists(), "未设 AGENTHUB_LOG 却落了日志（默认必须关闭）"
        os.environ["AGENTHUB_LOG"] = str(log)
        resp_ok(m.handle_message(rpc("tools/call", {"name": "hub_health",
                                                    "arguments": {"agent": "adv"}}), str(root)))
        lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert lines, "开启后未落任何日志"
        rec = lines[-1]
        for k in ("ts", "tool", "ms", "ok", "err_kind", "agent", "root"):
            assert k in rec, f"缺字段 {k}: {rec}"
        assert rec["tool"] == "hub_health" and rec["ok"] is True and rec["agent"] == "adv", rec
        assert rec["ms"] >= 0, rec
        m.handle_message(rpc("tools/call", {"name": "hub_get_record",
                                            "arguments": {"record_id": "not-an-int"}}), str(root))
        lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert any(x["err_kind"] for x in lines), f"异常路径未落痕/err_kind 全空: {lines}"
        os.environ["AGENTHUB_LOG"] = str(Path(root) / "no_such_dir" / "x.jsonl")
        r = resp_ok(m.handle_message(rpc("tools/call", {"name": "hub_health", "arguments": {}}), str(root)))
        assert "error" not in r, "日志写失败竟导致调用失败（观测绝不能影响主链路）"
    finally:
        if saved is None:
            os.environ.pop("AGENTHUB_LOG", None)
        else:
            os.environ["AGENTHUB_LOG"] = saved


def t_readme_changelog_sync(root):
    """回归：README 版本历史表必须包含当前 SERVER_INFO 版本。

    这是本项目的**复发漏洞**——改代码时只改 SERVER_INFO、忘了同步 README，
    已在两次独立复盘里各抓到一次断档（第一次缺 v2.40.27/.28，第二次缺 v2.40.29~.39
    共 11 个版本）。补录只能治标，让回归替人盯住才治本：以后漏一次就红。
    """
    readme = Path(__file__).parent / "README.md"
    text = readme.read_text(encoding="utf-8")
    ver = m.SERVER_INFO["version"]
    assert f"| v{ver} |" in text, (
        f"README 版本历史表缺 v{ver} —— 改了 SERVER_INFO 就要同步 changelog"
        f"（本项目已两次断档，此断言就是为了防止第三次）")
    # 顺带守住版本号格式本身：必须是 x.y.z 三段数字（避免手滑写成 2.40 或 2.40.39.1）
    import re as _re
    assert _re.fullmatch(r"\d+\.\d+\.\d+", ver), f"版本号格式异常：{ver!r}"


def t_tool_annotations(root):
    """C1 回归：MCP 工具 annotations（host 靠它决定确认 UX——省略会让 host 退回
    「每次都问用户」的保守默认，用户烦了就把 server 关掉）。
    验证：①非写工具必有 readOnlyHint；②HIGH_BLAST_TOOLS 全部带 destructiveHint；
    ③只读与破坏性互斥、只读标幂等无意义；④派生集合关系成立（不出现第二份清单）；
    ⑤端到端 tools/list 输出里确实带 annotations（不只存在于内存对象）。"""
    for t in m.TOOLS:
        ann = t.get("annotations") or {}
        name = t["name"]
        if name not in m.WRITE_TOOLS:
            assert ann.get("readOnlyHint") is True, f"只读工具 {name} 缺 readOnlyHint"
            assert not ann.get("destructiveHint"), f"{name} 只读却标破坏性"
            assert not ann.get("idempotentHint"), f"{name} 是只读，标幂等无意义"
        else:
            assert not ann.get("readOnlyHint"), f"写工具 {name} 不该标 readOnlyHint"
    for name in m.HIGH_BLAST_TOOLS:
        ann = next(x for x in m.TOOLS if x["name"] == name).get("annotations") or {}
        assert ann.get("destructiveHint") is True, f"高危工具 {name} 缺 destructiveHint"
    assert m.HIGH_BLAST_TOOLS <= m.WRITE_TOOLS, "高危集合必须是写工具子集"
    assert m.IDEMPOTENT_TOOLS <= m.WRITE_TOOLS, "幂等标注只应对写工具有意义"
    tools = resp_ok(m.handle_message(rpc("tools/list"), str(root)))["tools"]
    assert tools, "tools/list 返回空"
    ro = [t for t in tools if t["name"] not in m.WRITE_TOOLS]
    assert ro and all((t.get("annotations") or {}).get("readOnlyHint") for t in ro), \
        "端到端输出里只读工具没带 readOnlyHint"
    hb = [t for t in tools if t["name"] in m.HIGH_BLAST_TOOLS]
    assert hb and all((t.get("annotations") or {}).get("destructiveHint") for t in hb), \
        "端到端输出里高危工具没带 destructiveHint"


_SCHEMA_BASELINE = Path(__file__).parent / "tool_schema_baseline.json"


def t_tool_schema_compat(root):
    """C2 回归：工具 schema 只允许**加法式**变更（新增可选参数是安全的）。
    静默删除/改名/改类型的参数是 MCP 的真实事故——客户端在会话开始时就缓存了
    tools/list 的 schema，服务端原地改参数会让它们静默失败（Microsoft Learn server
    事故：单个参数重命名挂掉 2–5% 客户端）。
    基线 `tool_schema_baseline.json` 是「参数下限」：可以加，不可以减/改。
    确需破坏性变更时，必须**显式更新基线文件**并说明——这就是「显式确认」的落点。"""
    assert _SCHEMA_BASELINE.is_file(), f"缺基线文件 {_SCHEMA_BASELINE}"
    base = json.loads(_SCHEMA_BASELINE.read_text(encoding="utf-8"))
    problems = []
    for t in m.TOOLS:
        name = t["name"]
        sch = t.get("inputSchema") or {}
        props = sch.get("properties") or {}
        cur = {k: (v or {}).get("type", "") for k, v in props.items()}
        cur_req = set(sch.get("required") or [])
        if name not in base:
            problems.append(f"{name}: 新工具，需补进基线")
            continue
        b = base[name]
        for k, ty in b["props"].items():
            if k not in cur:
                problems.append(f"{name}: 参数 {k} 被删除（客户端会静默失败）")
            elif cur[k] != ty:
                problems.append(f"{name}.{k}: 类型 {ty} → {cur[k]}（客户端传旧类型会失败）")
        for k in set(b["required"]) - cur_req:
            problems.append(f"{name}: 必填 {k} 被降级为可选（旧调用可能缺参）")
        for k in cur_req - set(b["required"]):
            problems.append(f"{name}: 新增必填 {k}（旧客户端不传会被拦）")
    assert not problems, "工具 schema 出现非加法式变更：\n  " + "\n  ".join(problems[:15])


@contextlib.contextmanager
def _rate_env(**kw):
    """临时改速率限制环境变量，退出即还原（回归自己不能被限流器误伤）。"""
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def t_idem_key(hub):
    """D1/R2 回归：写工具**幂等键**——「超时重试=重复记账」是 agent 的必然故障面
    （外部研究一致点名的写工具第一优先项：CallSphere/Thomas De Vos/datarekha）。
    验证：① 无键调用行为不变（兼容性由其它用例覆盖）；② 同 agent+键+内容重试 →
    原样重放首次结果、记录条数不变；③ 同键配不同内容 → 冲突拒收、不落写（防键复用）；
    ④ 不同 agent 的同字符串键互不干扰（命名空间）；⑤ memory_write / note 同样生效。"""
    import brain as _b
    rs = str(hub)
    base = {"project": "幂等-测试", "agent": "zcode",
            "content": "【目的】幂等\n【做了什么】写一次\n【验证结果】ok\n【如何回滚】删",
            "idem_key": "k1"}
    r1 = m.call_tool("hub_log_work", dict(base), rs)
    assert "records#" in r1, r1
    n1 = _b.records_count(rs, project="幂等-测试")
    # ② 重试同键同内容 → 原样重放，不新增
    r2 = m.call_tool("hub_log_work", dict(base), rs)
    assert r2.startswith(r1) and "幂等重放" in r2, f"未重放首次结果：{r2[:120]}"
    assert _b.records_count(rs, project="幂等-测试") == n1, "重试不该新增记录（重复记账）"
    # ③ 同键不同内容 → 冲突拒收（digest 防键复用）
    bad = dict(base, content=base["content"] + "（内容已变）")
    r3 = m.call_tool("hub_log_work", bad, rs)
    assert r3.startswith("错误：") and "不同内容" in r3, r3
    assert _b.records_count(rs, project="幂等-测试") == n1, "冲突拒收不该落写"
    # ④ 不同 agent 同键 → 互不干扰
    other = dict(base, agent="dsh")
    r4 = m.call_tool("hub_log_work", other, rs)
    assert "records#" in r4, r4
    assert _b.records_count(rs, project="幂等-测试") == n1 + 1
    # ⑤ 其它两个内容类写工具同样生效
    ma = {"content": "幂等键测试记忆：重试不重复写入", "kind": "fact", "agent": "zcode",
          "idem_key": "m1"}
    r5 = m.call_tool("hub_memory_write", dict(ma), rs)
    assert "已写入大脑记忆" in r5, r5
    r6 = m.call_tool("hub_memory_write", dict(ma), rs)
    assert "幂等重放" in r6 and r6.startswith(r5), r6
    na = {"content": "幂等随记：重试不重复", "idem_key": "n1"}
    r7 = m.call_tool("hub_note", dict(na), rs)
    r8 = m.call_tool("hub_note", dict(na), rs)
    assert "随记 #" in r7 and "幂等重放" in r8 and r8.startswith(r7), (r7[:80], r8[:80])
    # ⑥ 过期键不重放（保留期 7 天）：把键的 ts 改老 → 同键视为新操作
    with _b.db_conn(rs) as conn:
        conn.execute("UPDATE idem_keys SET ts='2020-01-01T00:00:00' WHERE agent='zcode' AND key IN ('m1','n1')")
    r9 = m.call_tool("hub_memory_write", dict(ma), rs)
    assert "已写入大脑记忆" in r9 and "幂等重放" not in r9, f"过期键被重放：{r9[:120]}"


def t_schema_dialect_safety(root):
    """D4/R4 回归：JSON Schema **方言安全守卫**——把「刻意不声明 $schema」守成事实。

    背景：MCP 2026-07-28 的 inputSchema 用 JSON Schema 2020-12。本大脑对外仍报
    2024-11-05（六个接入），且实测 69 个工具的 inputSchema **零方言特有构造**
    （无 $defs / prefixItems / dependentSchemas / unevaluated* / $dynamic*），属两种
    方言的公共子集——不声明 $schema 时客户端按各自默认方言校验都能通过 = 最大兼容。
    一旦有人（或某次「顺手现代化」）引入 2020-12 特有关键字、或硬写一行
    "$schema": ".../draft/2020-12/schema"，只认 draft-07 的接入会当场拒整个工具列表。

    验证三面：① 不出现任何 $schema 声明（当前有意选择）；
    ② 不出现任一 2020-12 特有关键字（把命中的关键字列全，方便修的人直接看是哪些）；
    ③ **反向验证**：把 $schema 声明和一个毒关键字分别注入到副本的不同深度，
    本守卫必须都抓到（守卫自己失灵 = 静默失效，这是本套件的惯例）。"""
    _2020_ONLY = ("$defs", "prefixItems", "dependentSchemas", "dependentRequired",
                  "unevaluatedItems", "unevaluatedProperties", "$dynamicRef",
                  "$dynamicAnchor", "contentSchema", "$vocabulary")

    def _scan(tools):
        """返回 (有无 $schema 声明, 命中的 2020-12 关键字集合)。"""
        hit_schema, hits = False, set()
        stack = list(tools)
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if "$schema" in node:
                    hit_schema = True
                for kw in _2020_ONLY:
                    if kw in node:
                        hits.add(kw)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return hit_schema, hits

    hs, hits = _scan(m.TOOLS)
    assert not hs, "inputSchema 出现了 $schema 声明——会锁死客户端方言，见 agenthub_mcp.TOOLS 上方注释"
    assert not hits, f"inputSchema 用了 2020-12 特有关键字：{sorted(hits)}（只认 draft-07 的接入会拒收）"
    # ③ 反向验证：注入后必须被抓
    import copy
    poisoned = copy.deepcopy(m.TOOLS)
    poisoned[0]["inputSchema"]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    poisoned[1]["inputSchema"]["$defs"] = {"x": {"type": "string"}}
    poisoned[2]["inputSchema"]["properties"]["zzz"] = {"prefixItems": [{"type": "string"}]}
    ph, pv = _scan(poisoned)
    assert ph and {"$defs", "prefixItems"} <= pv, f"守卫失灵：注入后未能全部抓到（{ph}, {pv}）"


def t_list_cache_hints(hub):
    """D4/R4 回归：tools/list 的**缓存提示 + 确定性排序**（MCP 2026-07-28 SEP-2549）。

    来源：https://blog.modelcontextprotocol.io/posts/2026-07-28/ 与
    https://modelcontextprotocol.io/specification/draft/server/utilities/caching
    规范原话——list 响应「carry cache hints and a *deterministic order*, so clients
    can cache tool catalogs and keep upstream prompt caches stable across reconnects」。
    两层收益：缓存提示省掉重取（省 token/延迟），顺序稳定让上游 prompt cache 跨重连
    不失效（本大脑工具目录 19.3K tok、每会话推一次，命中与否是实打实的钱）。

    验证六面：① resultType="complete"（2026 规范对可缓存结果的要求）；
    ② ttlMs 存在且 >= 0（规范硬约束）；③ cacheScope="private"（可见集由 --profile
    决定=per-caller，标 public 会让共享网关串发工具目录）；
    ④ **逐字节确定性**（同进程连调 5 次 JSON 全等——这是 prompt cache 命中的前提）；
    ⑤ name 严格升序（不依赖 TOOLS 的声明顺序，后者随人维护而漂移）；
    ⑥ env 可调（AGENTHUB_LIST_TTL_MS 生效且能恢复默认值）。"""
    rs = str(hub)

    def _listed():
        return m.handle_message(rpc("tools/list"), rs)["result"]

    r = _listed()
    # ①②③ 缓存提示三件套
    assert r.get("resultType") == "complete", f"缺 resultType（2026 可缓存要求）：{sorted(r)}"
    assert isinstance(r.get("ttlMs"), int) and r["ttlMs"] >= 0, f"ttlMs 非法：{r.get('ttlMs')!r}"
    assert r.get("cacheScope") == "private", f"cacheScope 应为 private：{r.get('cacheScope')!r}"
    # ④ 逐字节确定性：连调 5 次全等（prompt cache 命中的前提）
    blob = json.dumps(r, sort_keys=True, ensure_ascii=False)
    for _ in range(4):
        assert json.dumps(_listed(), sort_keys=True, ensure_ascii=False) == blob, "tools/list 非确定性"
    # ⑤ name 严格升序 + profile 切片是保序子集
    names_admin = [t["name"] for t in r["tools"]]
    assert names_admin == sorted(names_admin), "admin 切片未按 name 升序"
    assert len(names_admin) == len(set(names_admin)), "工具名有重复"
    old_profile = m.SERVER_PROFILE
    try:
        m.SERVER_PROFILE = "worker"
        rw = _listed()
        assert rw["cacheScope"] == "private", "worker 下也该是 private"
        names_worker = [t["name"] for t in rw["tools"]]
        assert names_worker == sorted(names_worker), "worker 切片未按 name 升序"
        assert set(names_worker) <= set(names_admin), "worker 不该有 admin 之外的工具"
        # 切片是**保序子集**：顺序与全量一致（确定性 = 与切片方式无关）
        assert names_worker == [n for n in names_admin if n in set(names_worker)], "切片顺序与全量不一致"
        assert len(names_worker) < len(names_admin), "worker 应比 admin 少"
    finally:
        m.SERVER_PROFILE = old_profile
    # ⑥ env 可调：改 TTL 立即生效，恢复后回到默认
    old_env = os.environ.get("AGENTHUB_LIST_TTL_MS")
    try:
        os.environ["AGENTHUB_LIST_TTL_MS"] = "12345"
        assert _listed()["ttlMs"] == 12345, "TTL 未随 env 生效"
        os.environ["AGENTHUB_LIST_TTL_MS"] = "not-a-number"
        assert _listed()["ttlMs"] == 300000, "非法 env 应回落默认"
    finally:
        if old_env is None:
            os.environ.pop("AGENTHUB_LIST_TTL_MS", None)
        else:
            os.environ["AGENTHUB_LIST_TTL_MS"] = old_env
    assert _listed()["ttlMs"] == 300000, "恢复 env 后应回默认 300000"


def t_usage_trace_completeness(hub):
    """D6/R6 回归：使用留痕必须覆盖**全部工具**，否则使用数据本身就是治理的依据漏洞。

    「哪些工具真被用过」是 C7 profile 决策、D6 工具治理、D10 时间线共用的地基。
    此前 tool_calls 只记 4 个能力型只读工具（TRACE_READ_TOOLS），加上 op_audit（写）
    与 searches（检索）后，仍然只有 21/69 有证据——于是「48 个从未被调用」这个数字
    绝大部分是**留痕盲区的假象**。拿假象做治理会把在用工具当僵尸下线。

    验证四面：① 写工具 / 原留痕集合内的只读工具 / **原留痕集合外的只读工具**三类
    都进 tool_calls（第三类是本轮补的盲区，也是这个用例存在的理由）；
    ② 失败的调用不留痕（isError 语义：不是一次成功的使用）；
    ③ **fail-open**：留痕这一行写失败时绝不能影响调用本身（可观测性不能反噬业务）；
    ④ 全量留痕后，69 个工具里不存在「结构上不可能被留痕」的工具（每个工具名都能
    在 tool_calls 里占位——用一次调用证明这条路径通）。
    """
    import brain as _b
    rs = str(hub)

    def _traced(tool):
        with _b.db_conn(rs) as conn:
            return conn.execute("SELECT COUNT(*) FROM tool_calls WHERE tool=?",
                                (tool,)).fetchone()[0]

    def _call(name, args=None):
        return m.handle_message(rpc("tools/call", {"name": name, "arguments": args or {}}), rs)

    # ① 三类工具都必须留痕
    probes = [("hub_memory_write", {"content": "留痕测试记忆内容足够长以免被短内容短路",
                                    "kind": "fact", "agent": "zcode"}),   # 写工具（走 op_audit 那条路）
              ("hub_distill", {}),                                        # 原留痕集合内
              ("hub_status", {}),                                         # 原留痕集合**外**（本轮补的盲区）
              ("hub_get_rules", {})]                                      # 再取一个纯只读
    for name, args in probes:
        before = _traced(name)
        r = m.handle_message(rpc("tools/call", {"name": name, "arguments": args}), rs)
        assert "error" not in r, f"{name} 调用出错：{r}"
        assert _traced(name) > before, f"{name} 未被留痕（留痕盲区回来了）"
    # 写工具额外仍走 op_audit（审计语义含 undo 句柄，与 tool_calls 不重复、不互相替代）
    with _b.db_conn(rs) as conn:
        assert conn.execute("SELECT COUNT(*) FROM op_audit WHERE tool='hub_memory_write'").fetchone()[0] > 0, \
            "写工具仍须保留 op_audit（含 undo 句柄，与 tool_calls 语义不同）"

    # ② 失败的调用不留痕（isError = 没做成，不该计入使用）
    before_err = _traced("hub_get_record")
    bad = m.handle_message(rpc("tools/call", {"name": "hub_get_record", "arguments": {"id": "abc"}}), rs)
    assert bad["result"].get("isError") or "错误" in bad["result"]["content"][0]["text"], \
        f"本探针应失败：{bad}"
    assert _traced("hub_get_record") == before_err, "失败的调用不该被计入使用"

    # ③ fail-open：留痕写失败不能影响调用（可观测性不得反噬业务）
    _orig = _b.log_tool_call
    try:
        _b.log_tool_call = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("留痕写失败"))
        r = m.handle_message(rpc("tools/call", {"name": "hub_status", "arguments": {}}), rs)
        assert "error" not in r and r["result"]["content"], f"留痕失败竟然影响了调用：{r}"
    finally:
        _b.log_tool_call = _orig

    # ④ 结构完备：留痕代码路径对**所有**工具名都能执行（不是只有探针这几个能走通）
    try:
        _b.log_tool_call(rs, "hub_任意工具名", "zcode")
        with _b.db_conn(rs) as conn:
            n = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE tool='hub_任意工具名'").fetchone()[0]
        assert n == 1, "留痕表对任意工具名都该能落行"
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"留痕路径对任意工具名失败：{e}") from e


def t_error_taxonomy(root):
    """D8/R8 回归：**错误分类学**——三桶（transient / permanent / ambiguous）。

    研究依据（technologyonthe.net / itential）：生产 MCP 服务器必须把错误分成三桶并
    **告诉调用方该怎么反应**——transient 可退避重试、permanent 立即失败、ambiguous
    结果未知（幂等键是解药）。反模式是「所有错误都只塞一句文案、不给类别」，模型于是
    要么对永久性错误死循环重试、要么对暂时性错误直接放弃。这也是 R2 幂等键的配套：
    幂等键解决「能不能安全重试」，分类学解决「该不该重试」。

    验证六面：① 参数类错误 → permanent；② 限流 → transient；③ 未归类错误 → ambiguous
    （兜底，不假装知道）；④ `structuredContent.error_kind` 带分类（机器可读，且不破坏
    「错误：」开头的既有约定——59 个失败站点与全库断言都依赖它）；⑤ 成功调用无 error_kind；
    ⑥ **提示文案的差异化**：permanent 不追加任何「可重试」字样（多说一句就是噪音、
    还会诱导无意义重试），transient/ambiguous 追加且指向幂等键。
    """
    rs = str(root)

    def _call(name, args=None):
        return m.handle_message(rpc("tools/call", {"name": name, "arguments": args or {}}), rs)

    # ① 参数类错误 → permanent（重试同一个坏参数只会再错一次）
    r1 = _call("hub_get_record", {"id": "not-a-number"})
    txt1 = r1["result"]["content"][0]["text"]
    assert r1["result"]["isError"] and txt1.startswith("错误"), f"应失败：{txt1[:80]}"
    k1 = r1["result"].get("structuredContent", {}).get("error_kind")
    assert k1 == "permanent", f"参数错误应归 permanent，实得 {k1}：{txt1[:80]}"
    assert "重试" not in txt1, f"permanent 不该诱导重试：{txt1[-80:]}"

    # ② 限流 → transient（等窗口过去就能成）
    os.environ["AGENTHUB_RATE_TOOL_MAX"] = "1"
    try:
        _call("hub_status", {})                       # 第 1 次：占用唯一配额
        r2 = _call("hub_status", {})                  # 第 2 次：被拦
        txt2 = r2["result"]["content"][0]["text"]
        assert r2["result"]["isError"], f"应被限流拦下：{txt2[:80]}"
        k2 = r2["result"].get("structuredContent", {}).get("error_kind")
        assert k2 == "transient", f"限流应归 transient，实得 {k2}：{txt2[:80]}"
        assert "退避重试" in txt2, f"transient 必须给出可行动提示：{txt2[-100:]}"
    finally:
        os.environ.pop("AGENTHUB_RATE_TOOL_MAX", None)
        m._RATE_WINDOWS.clear()

    # ③/⑥ 分类函数本身的边界（不依赖具体工具，直接测三桶判定）
    assert m._error_kind("错误：project 必填") == "permanent"
    assert m._error_kind("错误：项目不存在") == "permanent"
    assert m._error_kind("错误：触发速率限制（工具 hub_status，agent=zcode）") == "transient"
    assert m._error_kind("错误：数据库被锁定，请稍后再试") == "transient"
    assert m._error_kind("错误：某种全新的未知故障形态") == "ambiguous", "未命中任何关键词应兜底为 ambiguous"
    # permanent 无提示；另两类的提示对写工具会指向幂等键（R2 的配套）
    assert m._error_hint("permanent", "hub_log_work") == "", "permanent 不追加提示"
    assert "idem_key" in m._error_hint("transient", "hub_log_work")
    assert "idem_key" in m._error_hint("ambiguous", "hub_log_work")
    # 只读工具的 ambiguous 提示不提幂等键（幂等只对写有意义），改为建议先查现状
    assert "idem_key" not in m._error_hint("ambiguous", "hub_search")
    assert "只读查询" in m._error_hint("ambiguous", "hub_search")

    # ⑤ 成功调用不带 error_kind
    r5 = _call("hub_status", {})
    assert not r5["result"].get("isError"), f"成功调用：{r5}"
    assert "error_kind" not in r5["result"].get("structuredContent", {}), "成功结果不该带错误分类"


def t_rate_limit_loop_guard(root):
    """C6 回归：速率限制/循环保护——外部检索点名的生产常见存根「无限流：agent 进循环
    可无限调用」，本大脑此前无任何节流。断言面：默认阈值放行日常频次 / 单工具超限即拦
    （可行动文案）/ 被拦不计额 / 窗口过期恢复 / 单 agent 全工具二级配额 / off 开关 /
    反向验证（关掉限流器后同一超限场景必须放行——证明断言测的是限流器不是摆设）。"""
    def call(name="hub_health"):
        r = m.handle_message(rpc("tools/call", {"name": name, "arguments": {}}), str(root))
        assert "error" not in r, r
        return r["result"]

    m._RATE_WINDOWS.clear()
    try:
        # ① 默认阈值（600/60s）下正常频次全部放行——日常 agent 与回归自身不受影响
        for _ in range(5):
            assert call()["isError"] is False

        # ② 单工具超限即拦（阈值调到 3：第 4 次必须被拦，且文案可行动）
        m._RATE_WINDOWS.clear()
        with _rate_env(AGENTHUB_RATE_TOOL_MAX=3):
            for _ in range(3):
                assert call()["isError"] is False
            r = call()
            assert r["isError"] is True, r
            assert "速率限制" in r["content"][0]["text"], r
            # ③ 被拦不计额：连拦 10 次后，桶里仍只有放行的 3 个时间戳
            #    （被拦的调用没有执行，计入窗口只会让恢复更难——口径写进 agenthub_mcp 注释）
            for _ in range(10):
                assert call()["isError"] is True
            assert len(m._RATE_WINDOWS[("tool", "-", "hub_health")]) == 3, \
                "被拦的调用不该计入窗口"

        # ④ 窗口过期恢复：把时间戳改老（模拟窗口滑过，不靠 sleep），立即放行
        m._RATE_WINDOWS.clear()
        with _rate_env(AGENTHUB_RATE_TOOL_MAX=2):
            assert call()["isError"] is False
            assert call()["isError"] is False
            assert call()["isError"] is True
            aged = deque(time.monotonic() - 61 for _ in m._RATE_WINDOWS[("tool", "-", "hub_health")])
            m._RATE_WINDOWS[("tool", "-", "hub_health")] = aged
            assert call()["isError"] is False

        # ⑤ 单 agent 全工具二级配额：两个不同工具合计到顶，第 3 次（换工具也逃不掉）被拦
        m._RATE_WINDOWS.clear()
        with _rate_env(AGENTHUB_RATE_TOOL_MAX=100, AGENTHUB_RATE_AGENT_MAX=2):
            assert call()["isError"] is False
            assert call("hub_status")["isError"] is False
            r = call("hub_health")
            assert r["isError"] is True and "全工具" in r["content"][0]["text"], r

        # ⑥ off 开关：整体关闭后打满也不拦，且不建窗口桶（零副作用）
        m._RATE_WINDOWS.clear()
        with _rate_env(AGENTHUB_RATE_LIMIT="off"):
            for _ in range(10):
                assert call()["isError"] is False
            assert not m._RATE_WINDOWS, "off 时不该建窗口桶"

        # ⑦ 反向验证：把限流器关掉，② 的超限场景必须放行——否则说明断言测的不是限流
        m._RATE_WINDOWS.clear()
        orig = m._rate_limit_on
        m._rate_limit_on = lambda: False
        try:
            with _rate_env(AGENTHUB_RATE_TOOL_MAX=1):
                for _ in range(4):
                    assert call()["isError"] is False
        finally:
            m._rate_limit_on = orig
    finally:
        m._RATE_WINDOWS.clear()
        for k in ("AGENTHUB_RATE_TOOL_MAX", "AGENTHUB_RATE_AGENT_MAX",
                  "AGENTHUB_RATE_LIMIT", "AGENTHUB_RATE_WINDOW"):
            os.environ.pop(k, None)


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_mcp_")
    root = Path(tmp) / "hub"
    build_hub(root)
    print(f"临时目录：{tmp}\n")
    case("交接协议工具（create/list/claim/重复认领拒绝/空态）", lambda: t_handoff_tool(root))
    case("MCP协议（握手/50工具/未知方法/异常自动登记/ping）", lambda: t_protocol(root))
    case("isError 正确性（成功=False / 缺参·未知工具·claim缺agent=True）", lambda: t_iserror_correctness(root))
    case("响应体积双封顶（4000预算截断+full_text≤16K / max_chars=0失控>64K兜底）", lambda: t_response_size_guard())
    case("协议健壮性（畸形请求-32600 / 异常不外泄路径SQL）", lambda: t_handle_message_robustness(root))
    case("集中式必填校验（缺参拦截/agent豁免/checkpoint豁免/透传覆盖）", lambda: t_required_validation(root))
    case("Round7·业务失败缺前缀静默成功（项目/条目/流程不存在→isError=True）", lambda: t_round7_iserror_notfound(root))
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
    case("Round8·协议帧编码加固（ensure_ascii=True纯ASCII帧/中文无损/工具描述无损）", lambda: t_protocol_encoding(root))
    case("Round10·hub_checkpoint 无task_id 不再#None（清晰必填提示/有tid不退化）", lambda: t_round10_checkpoint_no_tid(root))
    case("Round11·整数入参坏类型不再伪装服务端故障（hub_graph/hub_playbook 清晰整数提示/合法值零回归/登记归类）",
         lambda: t_round11_int_param_type(root))
    case("Round12·日期入参规范化（斜杠/非法日期/到期提醒静默失效修复+缺省零回归）",
         lambda: t_round12_date_normalize(root))
    case("Round13·18工具整数入参不再静默回落（集中校验+漂移守卫+supersedes语义）",
         lambda: t_round13_int_params_central(root))
    case("R3·错误信息三件套（必填/项目不存在/枚举/未知工具/坏根/缺content 均带示例或核对路径）",
         lambda: t_error_msg_three_part(root))
    case("R7·四段式模板软提示（缺段回显/不污染存储/todo 不提示）", lambda: t_template_nudge(root))
    case("B3·确认闸门破坏半径分级（默认不打断/严格模式高危需显式confirm/低危不受影响）",
         lambda: t_confirm_blast_radius(fresh_hub(tmp, "hub_b3")))
    case("B8·结构化可观测日志（默认关闭零副作用/字段齐全/异常落痕/写失败不影响主链路）",
         lambda: t_structured_log(fresh_hub(tmp, "hub_b8")))
    case("文档纪律·README 版本历史表必须含当前 SERVER_INFO 版本（防第三次 changelog 断档）",
         lambda: t_readme_changelog_sync(root))
    case("C1·MCP 工具 annotations（只读放行/高危 destructiveHint/幂等/端到端输出带注解）",
         lambda: t_tool_annotations(root))
    case("C2·工具 schema 兼容（只允许加法式变更；删参数/改类型/新增必填一律拦）",
         lambda: t_tool_schema_compat(root))
    case("C6·速率限制/循环保护（默认放行/单工具超限即拦/被拦不计额/窗口恢复/off开关/agent级配额/反向验证）",
         lambda: t_rate_limit_loop_guard(root))
    case("D1·写工具幂等键（重试原样重放不重复记/同键异内容拒收/agent 命名空间/过期不重放/memory+note 同效）",
         lambda: t_idem_key(fresh_hub(tmp, "hub_idem")))
    case("D4·tools/list 缓存提示+确定性排序（2026-07-28 SEP-2549：resultType/ttlMs≥0/private/逐字节一致/切片保序/env 可调）",
         lambda: t_list_cache_hints(fresh_hub(tmp, "hub_list")))
    case("D4·JSON Schema 方言安全守卫（不锁 $schema / 无 2020-12 特有关键字 + 反向验证）",
         lambda: t_schema_dialect_safety(root))
    case("D6·使用留痕全覆盖（写工具/原留痕内/原留痕外三类都留/失败不留/fail-open 不反噬业务）",
         lambda: t_usage_trace_completeness(fresh_hub(tmp, "hub_trace")))
    case("D8·错误分类学三桶（permanent不诱导重试/transient提示退避/ambiguous兜底+幂等键/error_kind机器可读）",
         lambda: t_error_taxonomy(root))
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("MCP ALL PASS")


if __name__ == "__main__":
    main()
