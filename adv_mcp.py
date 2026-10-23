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
import zipfile
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


def t_help_topics(root):
    """R9 + R9'（2026-10-21）：hub_help 场景主题可发现——六个主题各有步骤，未知主题给出可选项；
    运维手册必须真实存在且覆盖恢复/清理/发布/任务治理关键命令（防文档腐烂成空链接）。"""
    for topic, needle in (("start", "hub_log_work"), ("search", "eval_search"),
                          ("backup", "_AgentHub_备份清单"), ("tasks", "tasks_health"),
                          ("recover", "runtime_health"), ("release", "build.bat")):
        out = m.call_tool("hub_help", {"topic": topic}, str(root))
        assert needle in out, (topic, out[:200])
    recover_help = m.call_tool("hub_help", {"topic": "recover"}, str(root))
    assert "db_integrity" in recover_help and "§3.4" in recover_help, recover_help[:400]
    backup_help = m.call_tool("hub_help", {"topic": "backup"}, str(root))
    assert "drill" in backup_help and "backup-drill" in backup_help, backup_help[:600]
    # 客户端按 schema 先校验：主题只加进 HELP_TOPICS、没进 enum，就会在 handler 之前被挡成
    # 「must be equal to one of the allowed values」（R9 的 tasks 主题正是这样漏了）
    schema = next(t for t in m.TOOLS if t["name"] == "hub_help")["inputSchema"]
    assert set(schema["properties"]["topic"]["enum"]) == set(m.HELP_TOPICS), (
        schema["properties"]["topic"]["enum"], list(m.HELP_TOPICS))
    tasks_help = m.call_tool("hub_help", {"topic": "tasks"}, str(root))
    assert "hub_lock" in tasks_help and "续租" in tasks_help, tasks_help[:400]
    assert "lease_health" in tasks_help and "cli.py" in tasks_help, tasks_help[:500]
    assert "prune" in tasks_help, tasks_help[:500]
    assert "digest" in tasks_help and "stats" in tasks_help, tasks_help[:500]
    search_help = m.call_tool("hub_help", {"topic": "search"}, str(root))
    assert "任务" in search_help and "错误" in search_help, search_help[:400]
    bad = m.call_tool("hub_help", {"topic": "nope"}, str(root))
    assert "未知主题" in bad and "recover" in bad, bad
    base = m.call_tool("hub_help", {}, str(root))
    assert "场景帮助" in base and "backup" in base, base[-300:]
    doc = Path(__file__).parent / "docs" / "运维手册.md"
    assert doc.is_file(), "缺 docs/运维手册.md"
    text = doc.read_text(encoding="utf-8")
    for needle in ("备份、校验与恢复", "backup-prune", "cli.py release", "runtime_health",
                   "任务管控：看板、租约、依赖与追溯", "quick_check", "写入围栏",
                   "### 4.6 文件租约", "hub_lock action=acquire", "fence",
                   "cli.py locks", "lease_health", "hub_lock action=prune", "locks --prune",
                   "hub_history(kind=\"task\"", "python cli.py --root <root> tasks",
                   "### 4.11 全脑检索的覆盖范围", "### 4.12 流水页错误处置",
                   "hub_status(view=\"digest\"", "复制回滚方式",
                   "### 3.5 库损坏的识别与降级", "db_corrupt", "db_integrity",
                   "backup-drill", "恢复演练",
                   "### 5.1 规模与内存", "AGENTHUB_TOK_CACHE_MAX",
                   "## 11. 规则闸门：证据通道与探针", "gate_probe.py",
                   "auto_records", "evidence", "sid=probe-", "ASU_SKIP_BRAINLOG"):
        assert needle in text, f"运维手册缺关键步骤：{needle}"


def t_backup_prune_surface(root):
    """R7：hub_export backups/prune 工具面——盘点四家族；prune 默认预演不删，apply=true 才删并留 journal。"""
    hub = fresh_hub(Path(root), "mcp_prune")
    meta = hub / core.DIR_META
    sdir = meta / brain.BACKUP_DIR
    sdir.mkdir(parents=True, exist_ok=True)
    (meta / "backup").mkdir(parents=True, exist_ok=True)
    now = time.time()

    def mk(p, age):
        p.write_bytes(b"x" * 100)
        os.utime(p, (now - age * 86400, now - age * 86400))
        return p

    olds = [mk(sdir / f"brain-2026010{i}-000000.db", 30 + i) for i in range(1, 4)]
    newest = mk(sdir / "brain-20261020-000000.db", 1)
    listing = m.call_tool("hub_export", {"action": "backups"}, str(hub))
    assert "snapshot" in listing and "4 份" in listing, listing
    preview = m.call_tool("hub_export", {"action": "prune", "keep_snapshot": 1,
                                         "min_age_days": 7}, str(hub))
    assert "预演" in preview and "可清理 3 份" in preview, preview
    assert all(p.exists() for p in olds), "预演删了文件"
    done = m.call_tool("hub_export", {"action": "prune", "keep_snapshot": 1,
                                      "min_age_days": 7, "apply": True}, str(hub))
    assert "已清理 3 份" in done and "journal" in done, done
    assert not any(p.exists() for p in olds) and newest.exists(), done
    with brain.db_conn(str(hub)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM journal WHERE action='backup_prune'").fetchone()[0]
    assert n == 1, f"清理未留 journal：{n}"


def t_backup_drill_surface(tmp):
    """R61：恢复演练只读——默认最新整包；报完整性/数据量/时效/§3.4；坏包与缺库包拒绝；
    演练后现场 db/-wal/-shm 字节不变；空白空 path 给可执行提示。"""
    hub = fresh_hub(Path(tmp), "mcp_drill")
    m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                 "content": "演练用记录"}, str(hub))
    err, zip_path = brain.backup_db(str(hub))
    assert not err, err
    meta = hub / core.DIR_META

    def snapshot():
        return {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                for p in meta.glob("brain.db*")}

    before = snapshot()
    out = m.call_tool("hub_export", {"action": "drill"}, str(hub))
    for needle in ("恢复演练：可用", "quick_check ok", "未指定 path，取最新整包",
                   "数据：records", "§3.4", "未写现场库"):
        assert needle in out, (needle, out[:500])
    assert snapshot() == before, "演练改动了现场库文件"
    # 备份后又写一条：演练必须点名现场与备份的差异（恢复会回到备份时刻）
    m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                 "content": "备份之后的写入"}, str(hub))
    out2 = m.call_tool("hub_export", {"action": "drill", "path": zip_path}, str(hub))
    assert ("现场比备份新" in out2) or ("现场与备份数据量不同" in out2), out2
    bad = Path(tmp) / "bad.zip"
    bad.write_bytes(b"PK\x03\x04garbage")
    out3 = m.call_tool("hub_export", {"action": "drill", "path": str(bad)}, str(hub))
    assert out3.startswith("错误：") and ("损坏" in out3 or "读取失败" in out3), out3
    nod = Path(tmp) / "nod.zip"
    with zipfile.ZipFile(nod, "w") as zf:
        zf.writestr("x.txt", "x")
    out4 = m.call_tool("hub_export", {"action": "drill", "path": str(nod)}, str(hub))
    assert "缺少 brain.db" in out4, out4
    empty = fresh_hub(Path(tmp), "mcp_drill_empty")
    out5 = m.call_tool("hub_export", {"action": "drill"}, str(empty))
    assert "还没有备份" in out5 and "action=backup" in out5, out5


def t_sentry_report(root):
    """R8：hub_sentry action=report 只出自检报告（每条问题附修复建议），不广播信号。
    R66：进程旧于磁盘时报告首行必须是新鲜度警告——旧检查逻辑会误报（实测旧进程把
    zcode 的 auto_records 通道误判成「闸门失效」假阳性），照办那条修复建议反而会
    动坏正常配置。双向 monkeypatch，不依赖测试机当时的真实新旧状态。"""
    hub = fresh_hub(Path(root), "mcp_doctor")
    out = m.call_tool("hub_sentry", {"action": "report"}, str(hub))
    assert "一键自检" in out and "修复：" in out, out[:400]
    assert "已广播" not in out, "report 不应发广播"
    with brain.db_conn(str(hub)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert n == 0, f"report 不应写 signals：{n}"
    import core
    _orig = core.runtime_staleness
    try:
        core.runtime_staleness = lambda *a, **k: {"known": True, "count": 2,
                                                  "started": "2026-10-23T17:45:12"}
        out_stale = m.call_tool("hub_sentry", {"action": "report"}, str(hub))
        core.runtime_staleness = lambda *a, **k: {"known": False, "count": 0, "started": ""}
        out_fresh = m.call_tool("hub_sentry", {"action": "report"}, str(hub))
    finally:
        core.runtime_staleness = _orig
    assert out_stale.splitlines()[0].startswith("⚠") and "可能过期" in out_stale.splitlines()[0], \
        f"旧进程报告缺新鲜度警告：{out_stale[:200]}"
    assert not out_fresh.splitlines()[0].startswith("⚠"), \
        f"新鲜进程不应带警告：{out_fresh[:120]}"


def t_protocol(root):
    r = resp_ok(m.handle_message(rpc("initialize", {"protocolVersion": "2025-06-18"}), str(root)))
    assert r["protocolVersion"] == "2025-06-18" and r["serverInfo"]["name"] == "agenthub"
    # 版本号单一事实源：握手必须回显 SERVER_INFO，不在测试里再抄一份（抄一份=每轮发版必红）
    assert r["serverInfo"]["version"] == m.SERVER_INFO["version"]
    assert r["serverInfo"]["version"].count(".") == 2, "版本号应形如 x.y.z"
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
            "hub_task_add", "hub_task_update", "hub_task_retry", "hub_claim", "hub_tasks",
            "hub_next", "hub_crystallize",
            "hub_event_write", "hub_events",
            "hub_skill_match", "hub_skill_sync"} <= names, names
    # 工具数守卫：数字每变一次都要**显式改这里并说明**——它是「工具面无声膨胀」的闸门
    # （与 ops 检查项 profile_surface 的被动上下文预算互为补充：那个看体积，这个看个数）
    assert len(names) == 78  # ...；78=+hub_freeze（R48：全局写入冻结——事故止损先停写，读面照常）；77=+hub_task_approve（R45：完成审批 four-eyes）；76=+hub_task_retry（R42/R43：失败重试指数退避 + 死信复活——失败不是静默消失）；75=+hub_task_quality（R40：完成质量体检——done 不只是状态，还要看关得扎不扎实）；72=+hub_agent（2026-10-20 R6：接入状态机——七项核对+验证快照，「装上了」与「真接上」不再是同一句话）；73=+hub_memory_confirm（第五轮 R2：记忆复核闭环——stale 从「只标注」到「可确认」，不重写 updated）；74=+hub_lock（R11：文件租约——任务租约防撞任务，文件租约防撞文件）
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


def t_initialize_instructions(root):
    """R4：握手即拿到最小作业规程；说明不能提到不存在的工具（防改名后文案腐烂）。"""
    r = resp_ok(m.handle_message(rpc("initialize", {"protocolVersion": "2024-11-05"}), str(root)))
    ins = r.get("instructions")
    assert isinstance(ins, str) and ins.strip(), f"initialize 缺少 instructions：{r.keys()}"
    assert len(ins) <= 900, f"instructions 超预算：{len(ins)} 字符"
    must = ["hub_get_progress", "hub_memory_read", "hub_log_work", "hub_memory_write",
            "hub_ops_run", "hub_memory_confirm", "hub_memory_supersede",
            "hub_search_tools", "hub_call_tool", "hub_report_error"]
    missing = [t for t in must if t not in ins]
    assert not missing, f"instructions 未覆盖核心流程：{missing}"
    known = {t["name"] for t in m.TOOLS}
    ghosts = sorted(set(re.findall(r"hub_[a-z_]+", ins)) - known)
    assert not ghosts, f"instructions 提到不存在的工具：{ghosts}"
    # 协议字段是纯文本：不夹带 markdown 代码块/多段结构
    assert "```" not in ins and ins.count("\n") == 0, "instructions 应为单段纯文本"


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
    # R51：project 会参与目录定位（coverage_gate 等），穿越名一律拒收
    out = m.call_tool("hub_log_work", {"project": "..\\逃逸", "agent": "zcode", "content": "y"}, root_s)
    assert "错误" in out and "project" in out, out


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
    # R6 任务级撞车预警：会话不活跃但别人持有在办任务，也要看见；自己的任务不刷
    _, tid = brain.task_add(root, "迁移 500 条记忆", project="任务-项目")
    brain.task_claim(root, tid, "hermes")
    out = m.call_tool("hub_heartbeat", {"agent": "dsh", "project": "任务-项目"}, root)
    assert "[任务预警]" in out and "hermes" in out and "迁移 500 条记忆" in out, out
    _, tid2 = brain.task_add(root, "dsh 自己的活", project="任务-项目")
    brain.task_claim(root, tid2, "dsh")
    out = m.call_tool("hub_heartbeat", {"agent": "dsh", "project": "任务-项目"}, root)
    assert "dsh 自己的活" not in out.split("[今日任务]")[0], out
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at='2020-01-01T00:00:00' WHERE id=?", (tid,))
    out = m.call_tool("hub_heartbeat", {"agent": "dsh", "project": "任务-项目"}, root)
    assert "租约过期可接管" in out, out
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


def t_file_lease_surface(root):
    """R11：hub_lock 工具面——check/acquire/冲突/续租/释放/list/dry_run/参数校验/
    心跳预警与自动续租/看板锁计数/任务完成自动释放；worker 可见。"""
    rs = str(root)
    tool = next((t for t in m.TOOLS if t["name"] == "hub_lock"), None)
    assert tool is not None, "hub_lock 未注册"
    props = tool["inputSchema"]["properties"]
    assert {"action", "path", "lease_id", "task_id", "ttl_min", "reason",
            "include_released", "limit", "agent"} <= set(props), props
    assert "hub_lock" in m.PROFILE_WORKER and "hub_lock" in m.WRITE_TOOLS

    out = m.call_tool("hub_lock", {"path": "测试-项目/a.py"}, rs)
    assert "空闲" in out, out
    out = m.call_tool("hub_lock", {"action": "acquire", "path": "测试-项目/a.py",
                                   "agent": "zcode", "reason": "改解析器"}, rs)
    assert "已获取" in out and "fence 1" in out and "zcode" in out, out
    leases = brain.file_lease_list(rs, path="测试-项目/a.py")
    assert len(leases) == 1 and leases[0]["owner"] == "zcode", leases
    lid = leases[0]["id"]
    out = m.call_tool("hub_lock", {"path": "测试-项目/a.py"}, rs)
    assert "被占用" in out and "zcode" in out and "改解析器" in out, out
    out = m.call_tool("hub_lock", {"action": "acquire", "path": "测试-项目/a.py",
                                   "agent": "dsh"}, rs)
    assert "错误" in out and "zcode" in out and "不要并发编辑" in out, out
    out = m.call_tool("hub_lock", {"action": "renew", "lease_id": lid, "agent": "dsh"}, rs)
    assert "错误" in out and "只有持有者" in out, out
    out = m.call_tool("hub_lock", {"action": "renew", "lease_id": lid, "agent": "zcode",
                                   "ttl_min": 5}, rs)
    assert "已续" in out and "剩 5 分钟" in out, out
    out = m.call_tool("hub_lock", {"action": "list", "project": "测试-项目"}, rs)
    assert f"#{lid}" in out and "测试-项目/a.py" in out and "剩" in out, out
    # dry_run 不落库（走 handle_message 的写闸门，call_tool 直调会绕过）
    dr = resp_ok(m.handle_message(rpc("tools/call", {
        "name": "hub_lock",
        "arguments": {"action": "acquire", "path": "测试-项目/b.py",
                      "agent": "dsh", "dry_run": True}}), rs))
    assert "dry_run" in dr["content"][0]["text"] and dr["isError"] is False, dr
    assert brain.file_lease_list(rs, path="测试-项目/b.py") == [], "dry_run 不应落库"
    # 参数校验
    assert "错误" in m.call_tool("hub_lock", {"action": "acquire", "agent": "zcode"}, rs)
    assert "错误" in m.call_tool("hub_lock", {"action": "renew", "lease_id": "abc",
                                              "agent": "zcode"}, rs)
    assert "只支持" in m.call_tool("hub_lock", {"action": "nope", "agent": "zcode"}, rs)

    # 心跳预警：他人持锁可见，自己的锁不在自己心跳里刷
    out = m.call_tool("hub_lock", {"action": "acquire", "path": "测试-项目/c.py",
                                   "agent": "dsh", "reason": "改 GUI"}, rs)
    assert "已获取" in out, out
    out = m.call_tool("hub_heartbeat", {"agent": "zcode", "project": "测试-项目"}, rs)
    assert "[文件租约]" in out and "dsh" in out and "测试-项目/c.py" in out, out
    out = m.call_tool("hub_heartbeat", {"agent": "dsh", "project": "测试-项目"}, rs)
    assert "· 测试-项目/c.py" not in out, out

    # 心跳自动续租：只续挂在本人进行中任务上的锁
    _, tid = brain.task_add(rs, "锁文件任务", project="测试-项目")
    assert brain.task_claim(rs, tid, "zcode")[0] == ""
    out = m.call_tool("hub_lock", {"action": "acquire", "path": "测试-项目/task.py",
                                   "agent": "zcode", "task_id": tid}, rs)
    assert "已获取" in out and f"任务 #{tid}" in out, out
    tl = brain.file_lease_list(rs, path="测试-项目/task.py")[0]
    with brain.db_conn(rs) as conn:
        conn.execute("UPDATE file_leases SET expires_at='2020-01-01T00:00:00' WHERE id=?",
                     (tl["id"],))
    out = m.call_tool("hub_heartbeat", {"agent": "zcode", "project": "测试-项目"}, rs)
    assert "自动续文件租约 1 把" in out, out
    fresh = brain.file_lease_list(rs, path="测试-项目/task.py")[0]
    assert fresh["expires_at"] > "2021", fresh

    # 看板「锁 N」；任务 done → 挂在其上的锁自动释放
    out = m.call_tool("hub_tasks", {"project": "测试-项目"}, rs)
    assert "锁 1" in out, out
    out = m.call_tool("hub_task_update", {"task_id": tid, "status": "done",
                                          "agent": "zcode"}, rs)
    assert "已更新" in out, out
    fresh = brain.file_lease_list(rs, path="测试-项目/task.py", include_released=True)[0]
    assert fresh["released_at"], fresh
    out = m.call_tool("hub_lock", {"action": "release", "lease_id": lid, "agent": "zcode"}, rs)
    assert "已释放" in out, out
    assert brain.file_lease_check(rs, "测试-项目/a.py")["free"] is True

    # R13 过期租约回收：只动过期未释放；dry_run 不落库；活跃租约不受影响
    _, xp = brain.file_lease_acquire(rs, "测试-项目/expired.py", "dsh", ttl_min=1)
    with brain.db_conn(rs) as conn:
        conn.execute("UPDATE file_leases SET expires_at='2020-01-01T00:00:00' WHERE id=?",
                     (xp["id"],))
    dr = resp_ok(m.handle_message(rpc("tools/call", {
        "name": "hub_lock", "arguments": {"action": "prune", "dry_run": True}}), rs))
    assert "dry_run" in dr["content"][0]["text"] and dr["isError"] is False, dr
    assert brain.file_lease_list(rs, path="测试-项目/expired.py")[0]["released_at"] == "", \
        "dry_run 不应回收"
    out = m.call_tool("hub_lock", {"action": "prune", "agent": "zcode"}, rs)
    assert "已回收 1 条" in out and f"#{xp['id']}" in out, out
    fresh = brain.file_lease_list(rs, path="测试-项目/expired.py", include_released=True)[0]
    assert fresh["released_by"] == "expiry", fresh
    assert brain.file_lease_check(rs, "测试-项目/c.py")["free"] is False, "活跃租约不得被回收"
    out = m.call_tool("hub_lock", {"action": "prune"}, rs)
    assert "没有过期未释放" in out, out


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
        assert lines[0]["result"].get("instructions"), "真实子进程握手未返回 instructions"
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
    assert "全脑检索" in out and "命中 记录1 · 记忆0 · 随记0 · 任务0 · 错误0 · 文件0" in out, out
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
    # R56：任务/错误进全脑检索——「这个活/这个坑之前怎么处理」能直接搜到
    m.call_tool("hub_task_add", {"title": "排查 Everything 索引延迟", "project": "测试-项目",
                                 "detail": "索引延迟 3 秒", "agent": "zcode"}, root_s)
    m.call_tool("hub_report_error", {"title": "Everything 索引延迟", "agent": "zcode",
                                     "project": "测试-项目", "detail": "索引未及时刷新",
                                     "undo": "重建索引"}, root_s)
    out2 = m.call_tool("hub_search", {"keyword": "Everything 索引"}, root_s)
    assert "[任务#" in out2 and "[错误#" in out2, f"任务/错误未进检索：{out2}"
    assert "任务1" in out2 and "错误1" in out2, out2
    assert "回滚：重建索引" in out2, f"错误条目缺回滚方案：{out2}"
    assert "已修复" not in out2 and "[未关闭]" in out2, out2
    # 零命中也提示任务/错误查询入口
    assert "hub_list_errors" in m.call_tool("hub_search", {"keyword": "不存在的词zzq"}, root_s)
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
    """hub_archive_project（v2.7/R21）：归档成功（状态+目录）/未找到报错/重复归档报错/
    restore=true 一步恢复（状态+目录）/重复恢复与未归档报错。"""
    root = Path(tmp) / "hub_archive_tool"
    build_hub(root)
    root_s = str(root)
    out = m.call_tool("hub_archive_project", {"project": "测试-项目", "agent": "zcode"}, root_s)
    assert "已归档" in out and "99_Archive" in out, out
    assert "restore=true" in out, out
    assert (root / "99_Archive" / "测试-项目").is_dir(), "目录未移入归档区"
    assert not (root / "测试-项目").exists()
    out2 = m.call_tool("hub_archive_project", {"project": "测试-项目"}, root_s)
    assert "已是归档状态" in out2, out2
    out3 = m.call_tool("hub_archive_project", {"project": "不存在的项目xyz"}, root_s)
    assert "未找到" in out3, out3
    out3b = m.call_tool("hub_archive_project", {"project": "不存在的项目xyz", "restore": True}, root_s)
    assert "未找到" in out3b, out3b
    # R21 恢复通道：restore=true 一步回滚状态与目录
    out4 = m.call_tool("hub_archive_project",
                       {"project": "测试-项目", "restore": True, "agent": "zcode"}, root_s)
    assert "已恢复" in out4 and "active" in out4, out4
    assert (root / "测试-项目").is_dir()
    assert not (root / "99_Archive" / "测试-项目").exists()
    out5 = m.call_tool("hub_archive_project", {"project": "测试-项目", "restore": True}, root_s)
    assert "未归档" in out5, out5


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

        # ③ 筛选工具不下发（agent 参数是"查别人/全机"，语义不同）。
        # 2026-10-20：新增工具漏登记曾导致 hub_usage_timeline 永远只统计自己——
        # 逐个断言「显式 agent 原样保留 + 不传时不注入」，防再次漏网。
        for tname in ("hub_list_skills", "hub_bootstrap_status", "hub_skill_match", "hub_agent",
                      "hub_usage_timeline", "hub_events", "hub_skill_sync"):
            got = m.apply_server_agent(tname, {"agent": "zcode"}, rs)
            assert got["agent"] == "zcode", f"{tname} 的筛选参数被身份覆盖：{got}"
            got = m.apply_server_agent(tname, {}, rs)
            assert "agent" not in got, f"{tname} 未传 agent 却被注入身份：{got}"

        # ③b 端到端：锁定身份下 stats 默认仍是全机视角（不是只统计自己）
        with brain.db_conn(rs) as conn:
            for ag in ("zcode", "workbuddy"):
                conn.execute("INSERT INTO tool_calls(ts,agent,tool) VALUES(?,?,?)",
                             (brain._now(), ag, "probe_filter_tool"))
        out = m.call_tool("hub_usage_timeline", {"view": "stats", "days": 7}, rs)
        assert "zcode" in out and "workbuddy" in out, f"全机统计被身份过滤：{out[:300]}"

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
    old = brain.MAX_WIP_PER_AGENT
    brain.MAX_WIP_PER_AGENT = 0
    try:
        _t_p3_tasks_events_impl(tmp)
    finally:
        brain.MAX_WIP_PER_AGENT = old


def _t_p3_tasks_events_impl(tmp):
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

    # R1：due="-" 必须被放行并清除；释放时 blocked_by 自动清空；blocked 不能直接开工
    r4 = m.call_tool("hub_task_add", {"title": "清截止", "due": "2026-10-30", "agent": "zcode"}, rs)
    tid4 = int(r4.split("#")[1].split("：")[0])
    assert "已更新" in m.call_tool(
        "hub_task_update", {"task_id": tid4, "due": "-", "agent": "zcode"}, rs)
    with brain.db_conn(rs) as conn:
        assert conn.execute("SELECT due FROM tasks WHERE id=?", (tid4,)).fetchone()[0] == ""
    r5 = m.call_tool("hub_task_add", {"title": "清阻塞", "blocked_by": "旧依赖", "agent": "zcode"}, rs)
    tid5 = int(r5.split("#")[1].split("：")[0])
    assert "阻塞" in m.call_tool(
        "hub_task_update", {"task_id": tid5, "status": "in_progress", "agent": "zcode"}, rs)
    assert "已更新" in m.call_tool(
        "hub_task_update", {"task_id": tid5, "status": "pending", "agent": "zcode"}, rs)
    with brain.db_conn(rs) as conn:
        assert conn.execute("SELECT blocked_by FROM tasks WHERE id=?", (tid5,)).fetchone()[0] == ""

    # R2：无主/无租约的遗留 in_progress 可经 MCP 认领接管，提示要说明是接管
    r6 = m.call_tool("hub_task_add", {"title": "遗留无主", "agent": "zcode"}, rs)
    tid6 = int(r6.split("#")[1].split("：")[0])
    with brain.db_conn(rs) as conn:
        conn.execute("UPDATE tasks SET status='in_progress', owner='', claimed_at='' "
                     "WHERE id=?", (tid6,))
    out6 = m.call_tool("hub_claim", {"task_id": tid6, "agent": "dsh"}, rs)
    assert "无主任务接管成功" in out6 and "dsh" in out6, out6
    r7 = m.call_tool("hub_task_add", {"title": "遗留无租约", "agent": "zcode"}, rs)
    tid7 = int(r7.split("#")[1].split("：")[0])
    assert not brain.task_claim(rs, tid7, "zcode")[0]
    with brain.db_conn(rs) as conn:
        conn.execute("UPDATE tasks SET claimed_at='' WHERE id=?", (tid7,))
    out7 = m.call_tool("hub_claim", {"task_id": tid7, "agent": "dsh"}, rs)
    assert "租约缺失接管成功" in out7, out7
    # 无主任务：hub_next 可接管段点名原因；写检查点被拒（先认领）
    r8 = m.call_tool("hub_task_add", {"title": "无主前沿", "project": "测试-项目",
                                      "agent": "zcode"}, rs)
    tid8 = int(r8.split("#")[1].split("：")[0])
    with brain.db_conn(rs) as conn:
        conn.execute("UPDATE tasks SET status='in_progress', owner='', claimed_at='' "
                     "WHERE id=?", (tid8,))
    out8 = m.call_tool("hub_next", {"project": "测试-项目", "agent": "zcode"}, rs)
    assert "无主前沿" in out8 and "无主" in out8, out8
    out9 = m.call_tool("hub_checkpoint", {"task_id": tid8, "agent": "dsh",
                                          "content": "偷偷开工"}, rs)
    assert "无持有者" in out9 and "hub_claim" in out9, out9
    # R5：任务变更史（journal + 检查点 + 任务卡时点）可经 hub_history(kind=task) 追溯
    assert "检查点已存" in m.call_tool(
        "hub_checkpoint", {"task_id": tid6, "agent": "dsh", "content": "接管后进展"}, rs)
    # R27：让 updated 领先 progress_at（心跳续租的形态），推进与改动必须分成两个时点
    with brain.db_conn(rs) as conn:
        pa6 = conn.execute("SELECT progress_at FROM tasks WHERE id=?", (tid6,)).fetchone()[0]
        conn.execute("UPDATE tasks SET updated=? WHERE id=?",
                     ((datetime.datetime.fromisoformat(pa6)
                       + datetime.timedelta(hours=2)).isoformat(timespec="seconds"), tid6))
    hist6 = m.call_tool("hub_history", {"kind": "task", "id": tid6}, rs)
    assert ("变更史" in hist6 and "任务新建" in hist6 and "接管任务" in hist6
            and "检查点（manual）" in hist6), hist6
    # R27：时间口径四钟一行；updated 只作「最后改动」，推进必须单独可读
    assert "时间口径" in hist6 and "最后推进" in hist6 and "最后检查点" in hist6, hist6
    assert "agent 级" in hist6 and "不代表进展" in hist6, hist6
    # R3：依赖成环在 MCP 面被拒绝；非法 deps 不静默清空
    r10 = m.call_tool("hub_task_add", {"title": "环A", "agent": "zcode"}, rs)
    tid10 = int(r10.split("#")[1].split("：")[0])
    r11 = m.call_tool("hub_task_add", {"title": "环B", "deps": str(tid10),
                                       "agent": "zcode"}, rs)
    tid11 = int(r11.split("#")[1].split("：")[0])
    out11 = m.call_tool("hub_task_update",
                        {"task_id": tid10, "deps": str(tid11), "agent": "zcode"}, rs)
    assert "错误" in out11 and "成环" in out11, out11
    out_bad = m.call_tool("hub_task_add", {"title": "坏依赖", "deps": "abc",
                                           "agent": "zcode"}, rs)
    assert "错误" in out_bad and "无合法" in out_bad, out_bad

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


def t_unknown_tool_diagnostics(root):
    """R9：未知工具错误必须能自我纠正——三类真实原因各给不同动作，而非统一「去搜索」。"""
    root = str(root)
    # ① 拼错 → difflib 最近名（含经 hub_call_tool 转发的同一路径）
    out = m.call_tool("hub_memroy_write", {}, root)
    assert "你是不是要找" in out and "hub_memory_write" in out, out
    out = m.call_tool("hub_call_tool", {"name": "hub_memroy_write"}, root)
    assert "你是不是要找" in out and "hub_memory_write" in out, out
    # ② 带 MCP 全名 → 直接给本服务器内的准确名（不必再猜前缀）
    out = m.call_tool("mcp__agenthub__hub_health", {}, root)
    assert "MCP 全名" in out and "直接传 hub_health" in out, out
    # ③ 无法匹配 → 保留搜索与清单入口（不能只说「未知」）
    out = m.call_tool("zzz_no_such_tool_zzz", {}, root)
    assert "没有相近名" in out and "hub_search_tools" in out and "hub_help" in out, out
    # ④ 零命中检索 → 相近名 + 换更短名词的可行动提示
    out = m.call_tool("hub_search_tools", {"keyword": "hub_skill_matx"}, root)
    assert "0 命中不一定是没能力" in out and "hub_skill_match" in out, out


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
    # ⑦ R16 占位：另一请求已占位但未完成 → 不重复执行；超过保护期才允许接管
    pa = {"project": "幂等-测试", "agent": "zcode",
          "content": "【目的】占位\n【做了什么】未完成\n【验证结果】无\n【如何回滚】删",
          "idem_key": "p1"}
    digest = _b.idem_digest(m._idem_fields("hub_log_work", pa))
    with _b.db_conn(rs) as conn:
        conn.execute("INSERT INTO idem_keys(agent,key,tool,digest,result,ref,ts) "
                     "VALUES(?,?,?,?,'',NULL,?)",
                     ("zcode", "p1", "hub_log_work", digest, _b._now()))
    before = _b.records_count(rs, project="幂等-测试")
    rp = m.call_tool("hub_log_work", dict(pa), rs)
    assert "正在处理中" in rp and "records#" not in rp, rp
    assert _b.records_count(rs, project="幂等-测试") == before, "占位中不该重复执行"
    old_ts = (datetime.datetime.now()
              - datetime.timedelta(seconds=_b.IDEM_PENDING_GRACE_SEC + 10)
              ).isoformat(timespec="seconds")
    with _b.db_conn(rs) as conn:
        conn.execute("UPDATE idem_keys SET ts=? WHERE agent='zcode' AND key='p1'", (old_ts,))
    rp2 = m.call_tool("hub_log_work", dict(pa), rs)
    assert "records#" in rp2 and "正在处理中" not in rp2, rp2
    assert _b.records_count(rs, project="幂等-测试") == before + 1


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


def t_call_attribution(hub):
    """R28 控制面归因：tool_calls.agent 必须记「谁在调用」，而不是「在查谁」或空。

    背景：库内 417/622 条 tool_calls 归属为空，且筛选类工具（agent 参数语义是
    查询对象）会把 qoder 查 workbuddy 记成 workbuddy 调用——控制面拿这种数据做
    画像/治理，结论会整体偏。验证四层：
    ① --agent 锁定（生产接入主路径）最可信：调用参数里写别家也不改归属；
    ② 无锁定时用 initialize 的 clientInfo 兜底（旧接入不再产生空归属）；
    ③ 筛选类工具的查询对象绝不能被当成调用者（有无 clientInfo 都成立）；
    ④ 无锁定、无 clientInfo 的筛选调用记空——宁可空，不可错；
    ⑤ 同一身份兜底必须覆盖写入类工具：工作记录归属不能退化成 unknown；
       陌生客户端（未注册名）仍走 unknown 放行，不能因身份推断反而写不进去。
    """
    import brain as _b
    rs = str(hub)

    def _last_agent(tool):
        with _b.db_conn(rs) as conn:
            row = conn.execute(
                "SELECT agent FROM tool_calls WHERE tool=? ORDER BY id DESC LIMIT 1",
                (tool,)).fetchone()
        return row["agent"] if row else None

    old_srv, old_cli = m.SERVER_AGENT, m.CLIENT_NAME
    try:
        # ① 进程锁定身份优先（apply_server_agent 会覆盖入参，但日志读的是原 args）
        m.SERVER_AGENT, m.CLIENT_NAME = "qoder", "someoneelse"
        m.handle_message(rpc("tools/call", {
            "name": "hub_tasks", "arguments": {"agent": "workbuddy"}}), rs)
        assert _last_agent("hub_tasks") == "qoder", \
            f"锁定身份被入参覆盖：{_last_agent('hub_tasks')}"

        # ② 无锁定 → clientInfo 兜底（规范化："Qoder CLI" → qoder）
        m.SERVER_AGENT, m.CLIENT_NAME = "", ""
        m.handle_message(rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "Qoder CLI", "version": "test"}}), rs)
        assert m.CLIENT_NAME == "qoder", f"clientInfo 未规范化：{m.CLIENT_NAME!r}"
        m.handle_message(rpc("tools/call", {
            "name": "hub_status", "arguments": {}}), rs)
        assert _last_agent("hub_status") == "qoder", \
            f"clientInfo 兜底失效：{_last_agent('hub_status')!r}"

        # ⑤ 写入类工具同样吃 clientInfo 兜底（记录不再退化成 unknown）。
        # 临时库内置注册表没有 qoder，用 zcode 验证同一机制。
        content = "目的：验证归属\n做了什么：写一条记录\n验证结果：见断言\n如何回滚：删记录"
        m.CLIENT_NAME = "zcode"
        r = m.handle_message(rpc("tools/call", {
            "name": "hub_log_work",
            "arguments": {"project": "归属-项目", "content": content}}), rs)
        assert "error" not in r and not r["result"].get("isError"), r
        with _b.db_conn(rs) as conn:
            who = conn.execute("SELECT agent FROM records ORDER BY id DESC LIMIT 1").fetchone()["agent"]
        assert who == "zcode", f"clientInfo 未用于记录归属：{who!r}"
        m.SERVER_AGENT, m.CLIENT_NAME = "qoder", "zcode"
        m.handle_message(rpc("tools/call", {
            "name": "hub_log_work",
            "arguments": {"project": "归属-项目", "content": content}}), rs)
        with _b.db_conn(rs) as conn:
            who = conn.execute("SELECT agent FROM records ORDER BY id DESC LIMIT 1").fetchone()["agent"]
        assert who == "qoder", f"锁定身份未覆盖写入归属：{who!r}"
        m.SERVER_AGENT, m.CLIENT_NAME = "", "qoder"

        # ③ 筛选类工具：查询对象不得冒充调用者
        m.handle_message(rpc("tools/call", {
            "name": "hub_usage_timeline", "arguments": {"agent": "workbuddy"}}), rs)
        assert _last_agent("hub_usage_timeline") == "qoder", \
            f"查询对象被记成调用者：{_last_agent('hub_usage_timeline')}"

        # ④ 无任何身份线索时记空，不拿查询对象凑数
        m.CLIENT_NAME = ""
        m.handle_message(rpc("tools/call", {
            "name": "hub_usage_timeline", "arguments": {"agent": "workbuddy"}}), rs)
        assert _last_agent("hub_usage_timeline") == "", \
            f"无身份线索仍应记空：{_last_agent('hub_usage_timeline')!r}"

        # ⑥ 未注册客户端：不注入、不拒收，写入仍按 unknown 放行
        m.CLIENT_NAME = "cursor"
        r = m.handle_message(rpc("tools/call", {
            "name": "hub_log_work",
            "arguments": {"project": "归属-项目", "content": content}}), rs)
        assert "error" not in r and not r["result"].get("isError"), r
        with _b.db_conn(rs) as conn:
            who = conn.execute("SELECT agent FROM records ORDER BY id DESC LIMIT 1").fetchone()["agent"]
        assert who == "unknown", f"陌生客户端应保持 unknown：{who!r}"
    finally:
        m.SERVER_AGENT, m.CLIENT_NAME = old_srv, old_cli


def t_memory_supersede_exit(hub):
    """R9 回归：D5 的**执行路径**——`hub_memory_supersede`（两条既有记忆裁决谁胜出）。

    缺口由来：R5 让 ghost 检测给出确定性建议「留 #3188、退 #3187」，但**没有对应的
    调用去执行它**——agent 拿到建议后无处下手。现有 `hub_memory_write(supersedes=X)`
    是「新建一条取代 X」，会多造出第 3 条记忆，语义完全不是一回事（那是「我写了新结论
    取代旧结论」，这是「这两条都在库里，指定谁胜出」）。

    验证七面：① 裁决后 drop 那条退出检索（_live_sql 生效）、keep 那条仍在；
    ② 条目本体与证据链**保留可追溯**（不删数据，是取代不是删除）；
    ③ 撤销可行（反向再裁一次即可回到原状）；
    ④ 参数守卫（非正整数 / 自己取代自己 / 不存在的 id 都拒）；
    ⑤ 过 dry_run 与 confirm 闸门（它是写工具，不能绕过既有契约）；
    ⑥ journal 留痕（治理动作要可追溯）；
    ⑦ **语义分工正确**：本工具执行后记忆总数**不变**（与 write(supersedes=) 会 +1 区分开）。
    """
    import brain as _b
    rs = str(hub)
    body = "代理端口是 7890 这是本机长期生效的环境事实"

    def _live_ids():
        with _b.db_conn(rs) as conn:
            return {r["id"] for r in conn.execute(
                "SELECT id FROM memories WHERE " + _b._live_sql())}

    def _count():
        with _b.db_conn(rs) as conn:
            return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    a = int(_b.add_memory(rs, body + "（旧版 A）", kind="fact", agent="zcode", project="p"))
    b = int(_b.add_memory(rs, body + "（新版 B）", kind="fact", agent="zcode", project="p"))
    assert _live_ids() >= {a, b}, "两条都应在检索内（未裁决）"
    n0 = _count()

    # ⑤ 先过闸门：dry_run 不落任何东西
    rd = m.handle_message(rpc("tools/call", {"name": "hub_memory_supersede",
                                             "arguments": {"keep_id": b, "drop_id": a,
                                                           "dry_run": True}}), rs)["result"]
    assert "dry_run" in rd["content"][0]["text"] and _live_ids() >= {a, b}, "dry_run 不该落库"
    rc = m.handle_message(rpc("tools/call", {"name": "hub_memory_supersede",
                                             "arguments": {"keep_id": b, "drop_id": a,
                                                           "confirm": False}}), rs)["result"]
    assert "人工确认" in rc["content"][0]["text"] and _live_ids() >= {a, b}, "confirm=false 应被拦且不执行"

    # ① 正式裁决：b 胜出，a 退出检索
    r = m.handle_message(rpc("tools/call", {"name": "hub_memory_supersede",
                                            "arguments": {"keep_id": b, "drop_id": a}}), rs)["result"]
    assert not r.get("isError"), f"裁决失败：{r['content'][0]['text']}"
    live = _live_ids()
    assert b in live and a not in live, f"裁决未生效：keep={b in live} drop仍可见={a in live}"

    # ② 数据保留可追溯（取代 ≠ 删除）
    assert _count() == n0, "裁决不该删行（条目与证据链要能追溯）"
    with _b.db_conn(rs) as conn:
        row = conn.execute("SELECT superseded_by FROM memories WHERE id=?", (a,)).fetchone()
    assert row and int(row["superseded_by"] or 0) == b, f"取代链未落：{row}"

    # ⑥ journal 留痕（journal 表的列名是 action 不是 kind）
    with _b.db_conn(rs) as conn:
        hit = conn.execute("SELECT COUNT(*) FROM journal WHERE action='memory_supersede'").fetchone()[0]
    assert hit > 0, "治理动作必须进 journal（可追溯）"

    # ③ 撤销：反向再裁一次
    r2 = m.handle_message(rpc("tools/call", {"name": "hub_memory_supersede",
                                             "arguments": {"keep_id": a, "drop_id": b}}), rs)["result"]
    assert not r2.get("isError"), f"反向裁决失败：{r2['content'][0]['text']}"
    live2 = _live_ids()
    assert a in live2 and b not in live2, "反向裁决未生效（撤销路径断了）"
    assert _count() == n0, "来回裁决都不该改变总条数"

    # ④ 参数守卫
    for bad in ({"keep_id": 0, "drop_id": a}, {"keep_id": "abc", "drop_id": a},
                {"keep_id": a, "drop_id": 0}):
        rb = m.handle_message(rpc("tools/call", {"name": "hub_memory_supersede",
                                                 "arguments": bad}), rs)["result"]
        assert rb.get("isError") and rb["content"][0]["text"].startswith("错误"), f"坏参未拒：{bad}"
        assert rb["structuredContent"]["error_kind"] == "permanent", f"参数错应归 permanent：{bad}"
    same = m.handle_message(rpc("tools/call", {"name": "hub_memory_supersede",
                                               "arguments": {"keep_id": a, "drop_id": a}}), rs)["result"]
    assert same.get("isError") and "自己" in same["content"][0]["text"], "自己取代自己应拒"
    ghost = m.handle_message(rpc("tools/call", {"name": "hub_memory_supersede",
                                                "arguments": {"keep_id": 999999,
                                                              "drop_id": a}}), rs)["result"]
    assert ghost.get("isError"), "不存在的 id 应拒"

    # ⑦ 语义分工：与 write(supersedes=) 区分——本工具执行后总数不变
    n1 = _count()
    m.handle_message(rpc("tools/call", {"name": "hub_memory_supersede",
                                        "arguments": {"keep_id": a, "drop_id": b}}), rs)
    assert _count() == n1, "裁决后总数必须不变（新增/删除都不是它该做的事）"


def t_record_supersede_surface(hub):
    """v2.40.76：记录侧取代出口——ghost 检查项报出「高相似记录对」后，agent 能执行裁决。
    ① target=record 落取代链、drop 退出检索；② 正文保留，hub_get_record 明确标注取代关系；
    ③ 反向即撤销；④ 非法 target / 不存在 id 拒；⑤ journal 留痕；⑥ schema 声明 target 枚举。"""
    import brain as _b
    rs = str(hub)
    proj = "测试-项目"
    kw = "灰鲸迁移批次"
    a = int(_b.add_record(rs, proj, "zcode", "2026-10-18", "迁移旧版",
                          f"【目的】{kw}\n【做了什么】旧\n【验证结果】旧\n【如何回滚】无"))
    b = int(_b.add_record(rs, proj, "zcode", "2026-10-19", "迁移新版",
                          f"【目的】{kw}\n【做了什么】新\n【验证结果】新\n【如何回滚】无"))

    def _live():
        with _b.db_conn(rs) as conn:
            return {r["id"] for r in conn.execute(
                "SELECT id FROM records WHERE " + _b._rec_live_sql())}

    assert {a, b} <= _live()
    r = m.handle_message(rpc("tools/call", {"name": "hub_memory_supersede",
                                            "arguments": {"target": "record",
                                                          "keep_id": b, "drop_id": a}}), rs)["result"]
    assert not r.get("isError"), r["content"][0]["text"]
    assert "记录" in r["content"][0]["text"]
    live = _live()
    assert b in live and a not in live, f"记录裁决未生效：{live}"
    rec = m.handle_message(rpc("tools/call", {"name": "hub_get_record",
                                              "arguments": {"record_id": a}}), rs)["result"]
    text = rec["content"][0]["text"]
    assert f"已被 #{b} 取代" in text and "旧" in text, text[:200]
    with _b.db_conn(rs) as conn:
        n = conn.execute("SELECT COUNT(*) FROM journal WHERE action='record_supersede'"
                         ).fetchone()[0]
    assert n > 0, "记录裁决必须留 journal"
    # 反向撤销
    r2 = m.handle_message(rpc("tools/call", {"name": "hub_memory_supersede",
                                             "arguments": {"target": "record",
                                                           "keep_id": a, "drop_id": b}}), rs)["result"]
    assert not r2.get("isError")
    live2 = _live()
    assert a in live2 and b not in live2, "反向裁决未生效"
    # 非法 target / 不存在
    bad = m.handle_message(rpc("tools/call", {"name": "hub_memory_supersede",
                                              "arguments": {"target": "paper",
                                                            "keep_id": a, "drop_id": b}}), rs)["result"]
    assert bad.get("isError") and "target" in bad["content"][0]["text"], bad
    missing = m.handle_message(rpc("tools/call", {"name": "hub_memory_supersede",
                                                  "arguments": {"target": "record",
                                                                "keep_id": 999999, "drop_id": a}}),
                               rs)["result"]
    assert missing.get("isError"), "不存在的记录应拒"
    spec = next(t for t in m.TOOLS if t["name"] == "hub_memory_supersede")
    enum = spec["inputSchema"]["properties"]["target"]["enum"]
    assert enum == ["memory", "record"], enum


def t_usage_timeline_surface(hub):
    """D10/R4 回归：`hub_usage_timeline` 工具面。

    覆盖：① 工具注册与 profile 切片（worker 可见 / reader 不可见——它是干活者的
    自查面，不是最小只读集）；② 只读契约（不进 WRITE_TOOLS → 无 dry_run/confirm 注入，
    annotations.readOnlyHint）；③ 两个视图（timeline 事件流 / stats 聚合）都有输出；
    ④ 未知 view 回落 timeline（不炸）；⑤ 整数入参走集中守卫（days/limit 坏类型 →
    清晰错误 + error_kind=permanent，不静默回落默认值）；⑥ 过滤参数透传；
    ⑦ **只读**：调用不写任何表（用 tool_calls 计数不变证明——注意：本工具自己的
    调用也会留痕，所以只能证明「除自身留痕外无其他写入」用前后差验证）。"""
    import brain as _b
    rs = str(hub)
    t = next(x for x in m.TOOLS if x["name"] == "hub_usage_timeline")

    # ① 注册与切片
    assert "hub_usage_timeline" not in m.WRITE_TOOLS, "只读工具不该进写清单"
    assert t.get("annotations", {}).get("readOnlyHint") is True, "只读提示必须显式"
    assert "hub_usage_timeline" in m.PROFILE_WORKER, "worker 切片要能看到（干活者自查面）"
    assert "hub_usage_timeline" not in m.PROFILE_READER, "reader 是最小集，不放自查面"
    assert "days" in t["inputSchema"]["properties"] and "view" in t["inputSchema"]["properties"]

    # 造点使用数据（直接写库，绕开 MCP——测的是查询面不是留痕面）
    with _b.db_conn(rs) as conn:
        conn.execute("INSERT INTO tool_calls(ts,agent,tool) VALUES(?,?,?)",
                     (_b._now(), "workbuddy", "hub_search"))
        conn.execute("INSERT INTO searches(ts,agent,tool,query,hits) VALUES(?,?,?,?,?)",
                     (_b._now(), "workbuddy", "hub_search", "零命中探针", 0))

    def _call(args):
        return m.handle_message(rpc("tools/call", {"name": "hub_usage_timeline",
                                                   "arguments": args}), rs)["result"]

    # ③ 两个视图
    rt = _call({"days": 7})
    assert not rt.get("isError") and "使用时间线" in rt["content"][0]["text"], rt["content"][0]["text"][:200]
    assert "hub_search" in rt["content"][0]["text"], "造的调用没出现在时间线里"
    rstats = _call({"days": 7, "view": "stats"})
    assert not rstats.get("isError"), rstats["content"][0]["text"][:200]
    for sec in ("四源总量", "归属覆盖", "按工具", "零命中检索"):
        assert sec in rstats["content"][0]["text"], f"stats 缺段：{sec}"
    assert "零命中探针" in rstats["content"][0]["text"], "零命中查询没进 stats"
    attr_line = next(l for l in rstats["content"][0]["text"].splitlines()
                     if l.startswith("归属覆盖"))
    assert "有主 1" in attr_line and "空 1" in attr_line, \
        f"归属覆盖应如实显示 1 条有主 / 1 条空（本工具自身调用无身份线索）：{attr_line}"

    # ④ 未知 view 回落 timeline 不炸
    rv = _call({"view": "bogus"})
    assert not rv.get("isError") and "使用时间线" in rv["content"][0]["text"], "未知 view 应回落"

    # ⑤ 整数守卫（days/limit 已登记 _INT_PARAMS）
    for bad in ({"days": "abc"}, {"limit": "xyz"}):
        rb = _call(bad)
        assert rb.get("isError") and "必须是整数" in rb["content"][0]["text"], f"坏参未清晰拒：{bad}"
        assert rb["structuredContent"]["error_kind"] == "permanent", f"入参错应归 permanent：{bad}"

    # ⑥ 过滤透传：不存在的 agent → 空态文案（不炸）
    rf = _call({"agent": "nobody-here"})
    assert not rf.get("isError") and "无使用事件" in rf["content"][0]["text"], rf["content"][0]["text"][:200]

    # ⑦ 只读证明：除本工具自身留痕外无其他写入
    with _b.db_conn(rs) as conn:
        before = {tbl: conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                  for tbl in ("op_audit", "searches", "journal")}
    _call({"days": 7, "view": "stats"})
    with _b.db_conn(rs) as conn:
        after = {tbl: conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                 for tbl in ("op_audit", "searches", "journal")}
    assert before == after, f"只读工具写了非 tool_calls 表：{before} → {after}"


def t_ghost_demotion_surface(hub):
    """R3 回归：同事实多版本处置在 MCP 面的**可见性**——brain 层排序对了还不够，
    读的人（LLM）必须能从文案看出「这条是旧版/那条折了旧版」，否则降序只是隐形重排。

    覆盖：① ghost 对（0.55–0.85）→ 输出里旧版行带「旧版，现行版为 #N」、新版行不带；
    ② 近重复（≥0.85 折叠）→ 留存行带「已折叠旧版 #N」；
    ③ 标注不破坏既有行格式（#id/[kind]/内容仍在，isError=False）。"""
    import brain as _b
    rs = str(hub)
    t_old, t_new = "2026-01-01T00:00:00", "2026-06-01T00:00:00"

    def _mk(content, ts):
        mid = int(_b.add_memory(rs, content, kind="fact", agent="zcode", project="p"))
        with _b.db_conn(rs) as conn:
            conn.execute("UPDATE memories SET created=?, updated=? WHERE id=?", (ts, ts, mid))
        return mid

    old = _mk("环境事实：代理端口是 7890（本机配置 旧记录）", t_old)
    new = _mk("环境事实：代理端口是 7891（本机配置 新记录）", t_new)
    r = m.handle_message(rpc("tools/call", {"name": "hub_memory_read",
                                            "arguments": {"query": "代理端口"}}), rs)["result"]
    assert not r.get("isError"), r["content"][0]["text"][:200]
    text = r["content"][0]["text"]
    line_old = next(l for l in text.splitlines() if f"#{old} " in l)
    line_new = next(l for l in text.splitlines() if f"#{new} " in l)
    assert "旧版" in line_old and f"现行版为 #{new}" in line_old, f"旧版行缺标注：{line_old}"
    assert "旧版" not in line_new, f"新版行被误标：{line_new}"

    # ② 近重复折叠的可见性
    n1 = _mk("代理端口是 7890 这是本机长期生效的环境事实", t_old)
    n2 = _mk("代理端口是 7890 这是本机长期生效的环境事实。", t_new)
    r2 = m.handle_message(rpc("tools/call", {"name": "hub_memory_read",
                                             "arguments": {"query": "代理端口是 7890"}}), rs)["result"]
    text2 = r2["content"][0]["text"]
    kept_line = next(l for l in text2.splitlines() if f"#{n2} " in l)
    assert f"已折叠旧版 #{n1}" in kept_line, f"折叠缺回溯标注：{kept_line}"
    assert f"#{n1} " not in text2.replace(kept_line, ""), "被折掉的旧版不该单独成行"


def t_agent_registry_surface(hub):
    """R5 回归：agent 准入只看注册表，不看客户端死枚举。

    ① 三个写工具的 agent 参数不再有 enum——新增 agent 只登记 config.agents.json
       （Qoder 接入时被死枚举挡在客户端，才逼出 hub_call_tool+每家风适配器）；
    ② 临时根登记 Qoder → 以 qoder 写入成功（服务端按注册表放行）；
    ③ 未登记名字仍被拒收，且文案列出可用名单（服务端校验没被一起拆掉）；
    ④ 拒绝是 permanent（模型可立即纠正，不是催它重试）。"""
    rs = str(hub)
    tools = resp_ok(m.handle_message(rpc("tools/list"), rs))["tools"]
    schemas = {t["name"]: t.get("inputSchema") or {} for t in tools}
    for name in ("hub_log_work", "hub_memory_write", "hub_memory_supersede"):
        prop = schemas[name]["properties"]["agent"]
        assert "enum" not in prop, f"{name}.agent 客户端死枚举未移除：{prop}"
        assert "注册表" in prop.get("description", ""), f"{name}.agent 未说明准入来源：{prop}"
    (Path(rs) / core.AGENTS_CONFIG_NAME).write_text(
        json.dumps({"agents": [{"agent": "Qoder"}]}, ensure_ascii=False), encoding="utf-8")
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "qoder",
                                       "content": "R5 注册表准入验证（无死枚举）"}, rs)
    assert "已记入大脑" in out, out
    res = m.handle_message(rpc("tools/call", {"name": "hub_log_work",
                                              "arguments": {"project": "测试-项目", "agent": "nosuchagent",
                                                            "content": "应被拒"}}, 2), rs)["result"]
    body = res["content"][0]["text"]
    assert res["isError"] is True and "不在注册表内" in body and "qoder" in body, body
    assert (res.get("structuredContent") or {}).get("error_kind") == "permanent", res


def t_agent_onboard_surface(hub):
    """R6 回归：接入状态机的 MCP 面。

    ① 未注册 agent 也照常出七项清单（诊断不能被准入校验挡住——新 agent 正是最需要它的人）；
    ② plan 只留非 ok 项，status 全量；
    ③ verify 落快照（agent_onboarding 一行、七步、备注），复查时说「上次验证」不再说「从未 verify」；
    ④ 未知 action 是清晰错误（列出合法值+示例），不是静默回落。"""
    rs = str(hub)
    ag = "QoderProbe"
    r = m.handle_message(rpc("tools/call", {"name": "hub_agent",
                                            "arguments": {"action": "status", "agent": ag}}), rs)["result"]
    text = r["content"][0]["text"]
    assert not r.get("isError"), text[:300]
    for label in ("注册表", "引导注入", "MCP", "规则闸门", "技能索引", "工作记录", "运行版本", "验证快照"):
        assert label in text, f"清单缺 {label}：{text}"
    assert "接入清单" in text and "从未 verify" in text and 'hub_agent(action="verify"' in text, text

    r = m.handle_message(rpc("tools/call", {"name": "hub_agent",
                                            "arguments": {"action": "plan", "agent": ag}}), rs)["result"]
    plan = r["content"][0]["text"]
    assert not r.get("isError"), plan[:300]
    assert "✓ 注册表" not in plan, f"plan 不该列已通过项：{plan}"
    assert "注册表" in plan and "下一步：" in plan, plan

    r = m.handle_message(rpc("tools/call", {"name": "hub_agent",
                                            "arguments": {"action": "verify", "agent": ag,
                                                          "note": "R6 测试快照"}}), rs)["result"]
    vtext = r["content"][0]["text"]
    assert not r.get("isError") and "已写验证快照" in vtext, vtext[:300]
    import brain as _b
    import core as _core
    snap = _b.onboarding_get(rs, ag)
    assert len(snap) == 1 and len(snap[0]["steps"]) == 8, snap
    assert snap[0]["note"] == "R6 测试快照", snap
    # R6（2026-10-21）：verify 必须绑定「此刻在跑的代码」指纹，而不是留空靠 mtime
    assert snap[0]["fingerprint"] == _core.SERVER_FINGERPRINT, snap[0]["fingerprint"]
    assert "绑定" in vtext and "源码文件指纹" in vtext, vtext
    # R23（2026-10-22）：快照必须记录代码树，跨树复查不得冒充过期/一致
    assert snap[0]["base"] == _core.SERVER_DIR, (snap[0]["base"], _core.SERVER_DIR)
    assert "代码树" in vtext, vtext

    r = m.handle_message(rpc("tools/call", {"name": "hub_agent",
                                            "arguments": {"action": "status", "agent": ag}}), rs)["result"]
    again = r["content"][0]["text"]
    assert "从未 verify" not in again and "上次验证" in again, again

    # R7：快照过期——验证之后代码又变了，快照不能继续冒充「已验证」
    _b.onboarding_save(rs, ag, True, snap[0]["steps"], "旧快照")
    with _b.db_conn(rs) as conn:
        conn.execute("UPDATE agent_onboarding SET ts='2020-01-01T00:00:00' WHERE agent=?",
                     (ag.lower(),))
    r = m.handle_message(rpc("tools/call", {"name": "hub_agent",
                                            "arguments": {"action": "status", "agent": ag}}), rs)["result"]
    expired = r["content"][0]["text"]
    assert "快照已过期" in expired and 'hub_agent(action="verify"' in expired, expired

    # R7：运行版本落后（把启动时刻拨到 2020，并显式声明为 MCP 进程）——清单与进度页都必须当场告警
    import core as _core
    old_start = _core.SERVER_STARTED_AT
    old_flag = _core.SERVER_IS_MCP
    try:
        _core.SERVER_IS_MCP = True
        _core.SERVER_STARTED_AT = "2020-01-01T00:00:00"
        r = m.handle_message(rpc("tools/call", {"name": "hub_agent",
                                                "arguments": {"action": "status", "agent": ag}}), rs)["result"]
        rt = r["content"][0]["text"]
        assert "正在跑旧代码" in rt and "重启" in rt, rt
        r = m.handle_message(rpc("tools/call", {"name": "hub_get_progress",
                                                "arguments": {"limit": 3}}), rs)["result"]
        gp = r["content"][0]["text"]
        assert gp.lstrip().startswith("⚠") and "旧代码" in gp, gp[:200]
    finally:
        _core.SERVER_IS_MCP = old_flag
        _core.SERVER_STARTED_AT = old_start

    r = m.handle_message(rpc("tools/call", {"name": "hub_agent",
                                            "arguments": {"action": "bogus", "agent": ag}}), rs)["result"]
    body = r["content"][0]["text"]
    assert r.get("isError") is True and "status/plan/verify" in body, body[:300]


def t_agent_holdings_surface(hub):
    """R7 回归：Agent 控制面显示「谁手里压着活、租约是否有效」。

    ① hub_list_agents 在持有者行尾追加在办任务与租约徽章，空闲 agent 不出现噪音；
    ② hub_agent status 对该 agent 追加同一份持有摘要；
    ③ 无主在办单独点名（可接管），不能被算到任意 agent 名下。"""
    rs = str(hub)
    ag = "HoldProbe"
    m.handle_message(rpc("tools/call", {"name": "hub_heartbeat",
                                        "arguments": {"agent": ag, "project": "R7测试"}}), rs)
    m.handle_message(rpc("tools/call", {"name": "hub_heartbeat",
                                        "arguments": {"agent": "IdleProbe", "project": "R7测试"}}), rs)
    import brain as _b
    tid = _b.task_add(rs, "R7 持有面任务", project="R7测试")[1]
    assert _b.task_claim(rs, tid, ag)[0] == "", "测试前置：认领失败"
    listing = m.handle_message(rpc("tools/call", {"name": "hub_list_agents",
                                                  "arguments": {}}), rs)["result"]["content"][0]["text"]
    line = next((ln for ln in listing.splitlines() if ln.startswith("- holdprobe")), "")
    assert "在办 1 项" in line and f"#{tid}" in line and "租约剩" in line, listing
    assert "推进" in line and "检查点" not in line, listing  # R27 默认单行只带推进
    idle = next((ln for ln in listing.splitlines() if ln.startswith("- idleprobe")), "")
    assert idle and "在办 0/" in idle and "#" not in idle, listing

    out = m.handle_message(rpc("tools/call", {"name": "hub_agent",
                                              "arguments": {"action": "status", "agent": ag}}),
                           rs)["result"]["content"][0]["text"]
    assert "在办 1 项" in out and f"#{tid}" in out, out
    # R27：status 是详版——检查点与心跳（agent 级）都要露面，且心跳明确标注归属层级
    assert "检查点" in out and "心跳" in out and "（agent）" in out, out

    # R26：持有者在心跳在线、任务无推进时，控制面直接标「进展停滞」
    with _b.db_conn(rs) as conn:
        conn.execute("UPDATE tasks SET updated='2020-01-01T00:00:00', "
                     "progress_at='2020-01-01T00:00:00' WHERE id=?", (tid,))
    listing = m.handle_message(rpc("tools/call", {"name": "hub_list_agents",
                                                  "arguments": {}}), rs)["result"]["content"][0]["text"]
    line = next((ln for ln in listing.splitlines() if ln.startswith("- holdprobe")), "")
    assert "进展停滞" in line, listing

    tid2 = _b.task_add(rs, "R7 无主任务", project="R7测试")[1]
    with _b.db_conn(rs) as conn:
        conn.execute("UPDATE tasks SET status='in_progress', owner='', claimed_at='' WHERE id=?",
                     (tid2,))
    listing = m.handle_message(rpc("tools/call", {"name": "hub_list_agents",
                                                  "arguments": {}}), rs)["result"]["content"][0]["text"]
    assert "无主在办（可接管）" in listing and f"#{tid2}" in listing, listing


def t_task_board_surface(hub):
    """R8 回归：任务看板/前沿队列把 due、前置、租约、阻塞原因直接显示。

    过去 hub_tasks 只在 blocked 时显示 blocked_by，截止日只在 hub_next 的 ready 段出现，
    在办任务的租约缺失与 pending 上的残留阻塞标记完全不可见——看板看不出「为什么不能动」。"""
    import datetime as _dt
    rs = str(hub)
    import brain as _b
    today = _dt.date.today()
    dep = _b.task_add(rs, "R8 前置任务", project="R8")[1]
    ready = _b.task_add(rs, "R8 今天到期", project="R8", due=today.isoformat())[1]
    late = _b.task_add(rs, "R8 已逾期", project="R8",
                       due=(today - _dt.timedelta(days=2)).isoformat())[1]
    blocked = _b.task_add(rs, "R8 等别人", project="R8", blocked_by="等外部回复")[1]
    withdep = _b.task_add(rs, "R8 有前置", project="R8", deps=str(dep))[1]
    lostdep = _b.task_add(rs, "R8 前置不存在", project="R8", deps="999999")[1]
    lost = _b.task_add(rs, "R8 无租约在办", project="R8")[1]
    assert _b.task_claim(rs, lost, "boardprobe")[0] == ""
    stalled = _b.task_add(rs, "R26 心跳在线活停", project="R8")[1]
    assert _b.task_claim(rs, stalled, "boardprobe")[0] == ""
    starving = _b.task_add(rs, "R30 久置没人做", project="R8", priority=1)[1]
    with _b.db_conn(rs) as conn:
        conn.execute("UPDATE tasks SET claimed_at='' WHERE id=?", (lost,))
        conn.execute("UPDATE tasks SET updated='2020-01-01T00:00:00', "
                     "progress_at='2020-01-01T00:00:00' WHERE id=?", (stalled,))
        conn.execute("UPDATE tasks SET created=? WHERE id=?",
                     ((_dt.datetime.now()
                       - _dt.timedelta(days=_b.TASK_STARVE_DAYS + 2))
                      .isoformat(timespec="seconds"), starving))
    board = m.handle_message(rpc("tools/call", {"name": "hub_tasks",
                                                "arguments": {"project": "R8"}}),
                             rs)["result"]["content"][0]["text"]
    line = lambda tid: next(ln for ln in board.splitlines() if ln.startswith(f"- #{tid}"))
    assert "进展停滞" in line(stalled), board
    assert "待办饥饿" in line(starving), board
    assert "今天到期" in line(ready), board
    assert "逾期 2 天" in line(late), board
    assert "阻塞：等外部回复" in line(blocked), board
    assert "前置 0/1 完成" in line(withdep), board
    assert "前置不存在 #999999" in line(lostdep), board
    assert "租约缺失" in line(lost) and "@boardprobe" in line(lost), board

    nxt = m.handle_message(rpc("tools/call", {"name": "hub_next",
                                              "arguments": {"project": "R8"}}),
                           rs)["result"]["content"][0]["text"]
    assert f"#{ready}" in nxt and "今天到期" in nxt, nxt
    assert f"#{late}" in nxt and "逾期" in nxt, nxt
    assert "前置未完成" in nxt and f"#{dep}" in nxt, nxt
    assert f"#{lostdep}" in nxt and "不存在" in nxt, nxt
    assert "可接管" in nxt and f"#{lost}" in nxt, nxt
    assert "待办饥饿" in nxt and f"#{starving}" in nxt, nxt
    status = m.handle_message(rpc("tools/call", {"name": "hub_status",
                                                 "arguments": {}}),
                              rs)["result"]["content"][0]["text"]
    assert "待办饥饿" in status and f"#{starving}" in status, status


def t_freshness_surface(hub):
    """R4 回归：新鲜度标注在 MCP 面的**可见性**——brain 层算出状态还不够，
    hub_memory_read 的读的人（LLM）必须看到「这条多久没人复核/已过期」，
    否则读时状态机只是隐形重排、采信前毫无提示。

    覆盖五面：
    ① 30 天前的 fact（过 14 天新鲜窗）→ 行带「N 天未复核，采信前请确认仍现行」；
    ② 100 天前的 fact（过 90 天入仓期限）→ 行带「已过期…仅作历史证据」；
    ③ 昨天的 fact → 无任何新鲜度标注（不误报，噪音比漏报更贵）；
    ④ valid_until 已过的记忆 → 按 P1-1 契约**根本不进检索结果**（到期自动退出），
        MCP 的「已过期」文案实际覆盖年龄过期路径——契约边界写进测试防回归；
    ⑤ 标注不破坏既有行格式（#id/[kind]/内容仍在，isError=False）。"""
    import brain as _b
    import datetime as _dt
    rs = str(hub)
    now = _dt.datetime.now()
    d = lambda n: (now - _dt.timedelta(days=n)).isoformat(timespec="seconds")

    def _mk(content, days_ago, **cols):
        mid = int(_b.add_memory(rs, content, kind="fact", agent="zcode", project="p"))
        ts = d(days_ago)
        sets = {"created": ts, "updated": ts}
        sets.update(cols)
        with _b.db_conn(rs) as conn:
            conn.execute("UPDATE memories SET " + ", ".join(f"{k}=?" for k in sets)
                         + " WHERE id=?", list(sets.values()) + [mid])
        return mid

    def _line(text, mid):
        return next(l for l in text.splitlines() if f"#{mid} " in l)

    # ① 30 天前的 fact → stale 提示
    stale_id = _mk("环境事实：代理端口 7890 是三十天前记下的旧配置", 30)
    # ② 100 天前的 fact → age 过期（expire_days=90）
    expired_id = _mk("环境事实：代理端口 7892 于百日前记下且从未复核已深过期", 100)
    # ③ 昨天的 fact → fresh，不该有任何标注
    fresh_id = _mk("环境事实：代理端口 7891 是昨天刚确认的现行配置", 1)

    r = m.handle_message(rpc("tools/call", {"name": "hub_memory_read",
                                            "arguments": {"query": "代理端口"}}), rs)["result"]
    assert not r.get("isError"), r["content"][0]["text"][:200]
    text = r["content"][0]["text"]
    line_stale = _line(text, stale_id)
    assert "天未复核，采信前请确认仍现行" in line_stale, f"stale 行缺复核提示：{line_stale}"
    assert "旧版" not in line_stale, "stale 行不该混入版本标注（与 R3 职责分离）"
    line_expired = _line(text, expired_id)
    assert "已过期" in line_expired and "仅作历史证据" in line_expired, f"过期行缺标注：{line_expired}"
    line_fresh = _line(text, fresh_id)
    assert "未复核" not in line_fresh and "已过期" not in line_fresh, f"fresh 行被误标：{line_fresh}"
    # 行格式不破：编号/种类/内容三要素仍在
    for lid in (stale_id, expired_id, fresh_id):
        ln = _line(text, lid)
        assert f"#{lid} " in ln and "[环境事实]" in ln, f"行格式破损：{ln}"

    # ④ valid_until 过期 → 不进检索（P1-1 契约：到期自动退出；文案覆盖年龄路径）
    vu_id = _mk("环境事实：临时端口 9999 只在本周内有效", 1, valid_until="2020-01-01")
    r2 = m.handle_message(rpc("tools/call", {"name": "hub_memory_read",
                                             "arguments": {"query": "临时端口 9999"}}), rs)["result"]
    assert not r2.get("isError"), r2["content"][0]["text"][:200]
    assert f"#{vu_id} " not in r2["content"][0]["text"], "valid_until 过期仍被检索（契约 regression）"


def t_memory_confirm_tool(hub):
    """R5（第五轮 R2）回归：`hub_memory_confirm` MCP 面——把「复核」变成可执行动作。

    R4 的读时状态机已能标注 stale，但读的人**没有出口**：只能继续采信或自己重写一条
    记忆（后者会篡改 updated、制造重复）。本工具把「我还确认它成立」落成 confirmed
    时间戳，不碰内容、不碰 updated。

    验证面：
    ① 参数守卫（无选择器 / 坏 ids / 坏 kind / 坏 limit）→ permanent，且不落库；
    ② dry_run 与 confirm=false 两道闸门都拦得住；
    ③ ids 路径：确认后 confirmed 落库、检索标注转 fresh、内容与 updated 不变；
    ④ all_stale 路径：按 kind 过滤、limit 截断、remaining 如实上报；
    ⑤ valid_until 硬过期只跳过并说明原因（确认不能解除写入者划的线）；
    ⑥ journal 留痕 `memory_confirm`；记忆总数不变（复核不是新增/删除）。
    """
    import brain as _b
    import datetime as _dt
    rs = str(hub)
    now = _dt.datetime.now()
    old = (now - _dt.timedelta(days=30)).isoformat(timespec="seconds")

    def _mk(content, kind="fact", **cols):
        mid = int(_b.add_memory(rs, content, kind=kind, agent="zcode", project="p"))
        sets = {"created": old, "updated": old}
        sets.update(cols)
        with _b.db_conn(rs) as conn:
            conn.execute("UPDATE memories SET " + ", ".join(f"{k}=?" for k in sets)
                         + " WHERE id=?", list(sets.values()) + [mid])
        return mid

    def _call(args):
        return m.handle_message(rpc("tools/call", {"name": "hub_memory_confirm",
                                                    "arguments": args}), rs)["result"]

    def _conf(mid):
        with _b.db_conn(rs) as conn:
            return conn.execute("SELECT confirmed FROM memories WHERE id=?", (mid,)).fetchone()[0]

    def _reset_hit(*mids):
        """清掉 last_hit：读 s1 的检索可能经过语义路把 s2/s3 也「顺带唤起」，
        那会让它们以弱确认口径变 fresh，批量复核用例就测不到 stale 分支。"""
        with _b.db_conn(rs) as conn:
            conn.execute("UPDATE memories SET last_hit='' WHERE id IN ("
                         + ",".join("?" * len(mids)) + ")", mids)

    def _count():
        with _b.db_conn(rs) as conn:
            return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    s1 = _mk("环境事实：单条复核端口 7001（工具面）")
    s2 = _mk("环境事实：批量复核端口 7002（工具面）")
    s3 = _mk("环境事实：批量复核端口 7003（工具面）")
    lesson = _mk("踩坑经验：批量复核不得碰耐久知识（工具面）", kind="lesson")
    hard = _mk("环境事实：临时令牌只到昨天（工具面）", valid_until="2026-10-19")
    n0 = _count()

    # ① 参数守卫
    for bad, hint in (({}, "ids"), ({"ids": "abc"}, "整数"),
                      ({"ids": [s1], "kind": "nope"}, "kind"),
                      ({"all_stale": True, "limit": "abc"}, "整数")):
        rb = _call(bad)
        assert rb.get("isError"), f"坏参未拒：{bad}"
        assert hint in rb["content"][0]["text"], f"错误提示不可行动：{rb['content'][0]['text']}"
        assert rb["structuredContent"]["error_kind"] == "permanent", f"参数错应 permanent：{bad}"
    assert _conf(s1) == "" and _conf(s2) == "", "坏参路径写了库"

    # ② 闸门
    rd = _call({"ids": [s1], "dry_run": True})
    assert "dry_run" in rd["content"][0]["text"] and _conf(s1) == "", "dry_run 不该落库"
    rc = _call({"ids": [s1], "confirm": False})
    assert "人工确认" in rc["content"][0]["text"] and _conf(s1) == "", "confirm=false 应被拦"

    # ③ ids 路径
    r = _call({"ids": [s1]})
    assert not r.get("isError"), r["content"][0]["text"]
    assert "已确认" in r["content"][0]["text"], f"缺确认回执：{r['content'][0]['text']}"
    assert _conf(s1), "confirmed 未落库"
    with _b.db_conn(rs) as conn:
        row = dict(conn.execute("SELECT * FROM memories WHERE id=?", (s1,)).fetchone())
    assert row["content"].startswith("环境事实：单条复核端口 7001"), "确认改写了内容"
    assert row["updated"] == old, "确认改写了 updated（复核≠重写）"
    hit = next(x for x in _b.search_memories(rs, "单条复核端口 7001", "", 5, count_hits=False)
               if x["id"] == s1)
    assert hit["_freshness"] == "fresh", f"确认后检索仍标 {hit['_freshness']}"
    line = next(l for l in m.handle_message(
        rpc("tools/call", {"name": "hub_memory_read",
                           "arguments": {"query": "单条复核端口 7001"}}), rs
    )["result"]["content"][0]["text"].splitlines() if f"#{s1} " in l)
    assert "未复核" not in line, f"确认后仍提示复核：{line}"

    # ④ all_stale：kind 过滤 + limit + remaining
    _reset_hit(s2, s3, lesson)
    ra = _call({"all_stale": True, "kind": "fact", "limit": 1})
    assert not ra.get("isError"), ra["content"][0]["text"]
    assert _conf(s2) != "" and _conf(s3) == "", f"limit/排序未按预期：{ra['content'][0]['text']}"
    assert "剩余" in ra["content"][0]["text"], f"截断未上报 remaining：{ra['content'][0]['text']}"
    assert _conf(lesson) == "", "kind=fact 碰到了 lesson"
    ra2 = _call({"all_stale": True, "kind": "fact", "limit": 10})
    assert not ra2.get("isError"), ra2["content"][0]["text"]
    assert _conf(s3) != "", "第二轮未收敛剩余条目"
    assert _conf(lesson) == "", "lesson 仍不该被确认"

    # ⑤ valid_until 硬过期：跳过且说明原因
    rh = _call({"ids": [hard]})
    assert not rh.get("isError"), rh["content"][0]["text"]
    assert _conf(hard) == "", "valid_until 硬过期不该被确认"
    assert "valid_until" in rh["content"][0]["text"], f"未解释跳过原因：{rh['content'][0]['text']}"
    ra3 = _call({"all_stale": True, "kind": "fact", "limit": 10})
    assert "valid_until" in ra3["content"][0]["text"], "批量路径未报告硬过期跳过项"

    # ⑥ 留痕与条数
    with _b.db_conn(rs) as conn:
        hit_n = conn.execute("SELECT COUNT(*) FROM journal WHERE action='memory_confirm'").fetchone()[0]
    assert hit_n > 0, "复核动作必须进 journal"
    assert _count() == n0, "复核不该改变记忆总数"


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
    assert m._error_kind("错误：项目未归档，无需恢复：X（当前状态 active）") == "permanent"
    assert m._error_kind("错误：未恢复：根目录已有同名 X\\（未改动状态与文件）") == "permanent"
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


def t_new_project_hint(hub):
    """R28：首次写入的项目名给「新建 + 相近项目」提示（不阻断）；已登记项目静默。"""
    rs = str(hub)
    content = "目的：x\n做了什么：y\n验证结果：z\n如何回滚：w"
    # 空项目库（无目录、无 projects 行）首写：相近名计算曾因空列表 min(int) 抛
    # TypeError，把整条 log_work 拖垮——GUI test_journal_undo 抓到的真回归。
    bare = Path(rs).parent / "hint_bare"
    bare.mkdir()
    m.brain.init_db(str(bare))
    empty = m.call_tool("hub_log_work", {"project": "甲", "agent": "zcode",
                                         "content": content}, str(bare))
    assert "已记入大脑" in empty and "首次出现" in empty, empty
    first = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                         "content": content}, rs)
    assert "首次出现" not in first, first
    typo = m.call_tool("hub_log_work", {"project": "测试-項木", "agent": "zcode",
                                        "content": content}, rs)
    assert "首次出现" in typo and "测试-项目" in typo, typo
    again = m.call_tool("hub_log_work", {"project": "测试-項木", "agent": "zcode",
                                         "content": content}, rs)
    assert "首次出现" not in again, again


def t_log_work_task_binding(hub):
    """R35：hub_log_work 的 task 绑定——落库/项目一致/完成声明告警/持有者续租/状态页
    漂移可见/schema 可选参数/不绑定时零回归。"""
    rs = str(hub)
    err, tid = m.brain.task_add(rs, "MCP闭环任务", project="测试-项目", agent="zcode")
    assert not err, err
    out = m.call_tool("hub_log_work", {
        "project": "测试-项目", "agent": "zcode", "task": tid,
        "content": "目的：验证绑定\n做了什么：写实现\n验证结果：已完成并交付\n如何回滚：revert"},
        rs)
    assert f"任务 #{tid}" in out and "records#" in out, out
    assert "本条记录含完成声明" in out and "仍未关单" in out, out
    assert m.brain.task_get(rs, tid)["status"] == "pending", "绑定不得替代认领"
    with m.brain.db_conn(rs) as conn:
        row = conn.execute("SELECT task_id FROM records ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == tid, row[0]

    # 状态页点名闭环漂移
    st = m.call_tool("hub_status", {}, rs)
    assert "完成声明未关单" in st and f"任务#{tid}" in st, st

    # 持有者写入 = 续租 + 推进 progress_at（不改成 done）
    assert m.brain.task_claim(rs, tid, "zcode")[0] == ""
    with m.brain.db_conn(rs) as conn:
        conn.execute("UPDATE tasks SET claimed_at='2020-01-01T00:00:00', "
                     "progress_at='2020-01-01T00:00:00' WHERE id=?", (tid,))
    out2 = m.call_tool("hub_log_work", {
        "project": "测试-项目", "agent": "zcode", "task": tid,
        "content": "目的：续租\n做了什么：继续实现\n验证结果：单测通过\n如何回滚：revert"}, rs)
    assert "租约与进展已续" in out2, out2
    trow = m.brain.task_get(rs, tid)
    assert trow["status"] == "in_progress" and trow["claimed_at"] > "2020-01-01", trow
    assert trow["progress_at"] > "2020-01-01", trow

    # 项目不一致 / 不存在 / 坏类型：写前拒收（不落库）
    err2, tid_other = m.brain.task_add(rs, "别项目任务", project="另一个-项目", agent="zcode")
    assert not err2, err2
    bad_proj = m.call_tool("hub_log_work", {
        "project": "测试-项目", "agent": "zcode", "task": tid_other,
        "content": "目的：x\n做了什么：y\n验证结果：z\n如何回滚：w"}, rs)
    assert "不一致" in bad_proj, bad_proj
    miss = m.call_tool("hub_log_work", {
        "project": "测试-项目", "agent": "zcode", "task": 999999,
        "content": "目的：x\n做了什么：y\n验证结果：z\n如何回滚：w"}, rs)
    assert "不存在" in miss, miss
    bad_type = m.call_tool("hub_log_work", {
        "project": "测试-项目", "agent": "zcode", "task": "abc",
        "content": "目的：x\n做了什么：y\n验证结果：z\n如何回滚：w"}, rs)
    assert "整数" in bad_type and "省略" in bad_type, bad_type

    # 不绑定：零回归（输出不出现任务绑定文案）
    plain = m.call_tool("hub_log_work", {
        "project": "测试-项目", "agent": "zcode",
        "content": "目的：无绑定\n做了什么：普通记录\n验证结果：ok\n如何回滚：revert"}, rs)
    assert "已记入大脑" in plain and "任务 #" not in plain, plain

    schema = next(t for t in m.TOOLS if t["name"] == "hub_log_work")["inputSchema"]
    assert schema["properties"]["task"]["type"] == "integer", schema
    assert "task" not in (schema.get("required") or []), "task 必须是可选参数"


def t_agent_control_surface(hub):
    """R36：hub_agent(control) 读/切状态、替他人需 confirm、认领闸门、前沿告警、
    列表状态徽标与 schema 可选参数。"""
    rs = str(hub)
    out = m.call_tool("hub_agent", {"action": "control", "agent": "zcode"}, rs)
    assert "正常" in out and "active" in out, out
    denied = m.call_tool("hub_agent", {"action": "control", "agent": "zcode",
                                       "state": "draining"}, rs)
    assert "confirm=true" in denied, denied
    ok = m.call_tool("hub_agent", {"action": "control", "agent": "zcode",
                                   "state": "draining", "note": "收尾", "confirm": True}, rs)
    assert "收尾" in ok and "不接新任务" in ok, ok
    listed = m.call_tool("hub_list_agents", {}, rs)
    assert "控制:收尾" in listed, listed

    err, tid = m.brain.task_add(rs, "管控任务", project="测试-项目", agent="qoder")
    assert not err, err
    claim = m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)
    assert "收尾" in claim and "错误" in claim, claim
    nxt = m.call_tool("hub_next", {"agent": "zcode"}, rs)
    assert "已停止推荐新任务" in nxt, nxt

    back = m.call_tool("hub_agent", {"action": "control", "agent": "zcode",
                                     "state": "active", "confirm": True}, rs)
    assert "正常" in back, back
    claim2 = m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)
    assert "认领成功" in claim2, claim2

    bad = m.call_tool("hub_agent", {"action": "control", "agent": "zcode",
                                    "state": "dead", "confirm": True}, rs)
    assert "只能是" in bad, bad
    schema = next(t for t in m.TOOLS if t["name"] == "hub_agent")["inputSchema"]
    assert "control" in schema["properties"]["action"]["enum"], schema
    assert schema["properties"]["state"]["type"] == "string", schema
    assert "state" not in (schema.get("required") or []), schema


def t_skill_routing_surface(hub):
    """R37：hub_agent(skills) 读/设/清、hub_task_add skills、认领匹配与拒收、前沿过滤。"""
    rs = str(hub)
    setz = m.call_tool("hub_agent", {"action": "skills", "agent": "zcode",
                                     "skills": "python,文档", "confirm": True}, rs)
    assert "python" in setz and "文档" in setz, setz
    read = m.call_tool("hub_agent", {"action": "skills", "agent": "zcode"}, rs)
    assert "python" in read, read
    setb = m.call_tool("hub_agent", {"action": "skills", "agent": "buddy",
                                     "skills": "design", "confirm": True}, rs)
    assert "design" in setb, setb

    add = m.call_tool("hub_task_add", {"title": "Python 任务", "project": "测试-项目",
                                       "skills": "python"}, rs)
    tid = int(add.split("#")[1].split("：")[0].split()[0])
    denied = m.call_tool("hub_claim", {"task_id": tid, "agent": "buddy"}, rs)
    assert "不匹配" in denied, denied
    ok = m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)
    assert "认领成功" in ok, ok
    add2 = m.call_tool("hub_task_add", {"title": "设计任务", "project": "测试-项目",
                                        "skills": "design"}, rs)
    tid2 = int(add2.split("#")[1].split("：")[0].split()[0])
    nxt = m.call_tool("hub_next", {"agent": "zcode"}, rs)
    assert f"#{tid2}" not in nxt, nxt
    listed = m.call_tool("hub_tasks", {"project": "测试-项目"}, rs)
    assert "技能:python" in listed, listed

    clear = m.call_tool("hub_agent", {"action": "skills", "agent": "zcode",
                                      "skills": "-", "confirm": True}, rs)
    assert "已清除" in clear, clear
    schema = next(t for t in m.TOOLS if t["name"] == "hub_task_add")["inputSchema"]
    assert schema["properties"]["skills"]["type"] == "string", schema
    schema2 = next(t for t in m.TOOLS if t["name"] == "hub_agent")["inputSchema"]
    assert "skills" in schema2["properties"]["action"]["enum"], schema2



def t_task_stats_surface(hub):
    """R53 hub_tasks view=stats：流量/周期/在办卡点/重试/风险一张单，坏参与 schema 受控。"""
    rs = str(hub)
    now = datetime.datetime.now()
    iso = lambda h: (now - datetime.timedelta(hours=h)).isoformat(timespec="seconds")
    a = m.call_tool("hub_task_add", {"title": "统计完成单", "project": "测试-项目"}, rs)
    tid = int(a.split("#")[1].split("：")[0].split()[0])
    m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)
    with brain.db_conn(rs) as conn:
        conn.execute("UPDATE tasks SET created=?, claimed_at=? WHERE id=?",
                     (iso(10), iso(9), tid))
    done = m.call_tool("hub_task_update", {"task_id": tid, "status": "done", "agent": "zcode"}, rs)
    assert "完成" in done, done
    b = m.call_tool("hub_task_add", {"title": "等审批单", "project": "测试-项目",
                                     "approval_required": True}, rs)
    tid2 = int(b.split("#")[1].split("：")[0].split()[0])
    m.call_tool("hub_claim", {"task_id": tid2, "agent": "zcode"}, rs)
    c = m.call_tool("hub_task_add", {"title": "无主单", "project": "测试-项目"}, rs)
    tid3 = int(c.split("#")[1].split("：")[0].split()[0])
    d = m.call_tool("hub_task_add", {"title": "死信单", "project": "测试-项目"}, rs)
    tid4 = int(d.split("#")[1].split("：")[0].split()[0])
    with brain.db_conn(rs) as conn:
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (iso(3), tid2))
        conn.execute("UPDATE tasks SET status='in_progress', owner='', claimed_at='', "
                     "updated=? WHERE id=?", (iso(1), tid3))
        conn.execute("UPDATE tasks SET status='dead', owner='zcode', attempts=2, "
                     "last_error='boom', updated=? WHERE id=?", (iso(1), tid4))
    out = m.call_tool("hub_tasks", {"view": "stats", "days": 7, "project": "测试-项目"}, rs)
    for needle in ("任务过程统计", "完成周期", "当前在办", "租约超时 1", "无主 1",
                   "等审批 1", "重试存量", "按 agent", "风险信号", "死信 1"):
        assert needle in out, (needle, out[:500])
    assert "完成质量" in out, out[:500]
    empty = m.call_tool("hub_tasks", {"view": "stats", "project": "不存在的项目"}, rs)
    assert "风险信号：无" in empty and "完成质量" in empty, empty
    bad = m.call_tool("hub_tasks", {"view": "nope"}, rs)
    assert "错误" in bad and "board/stats" in bad, bad
    bid = m.call_tool("hub_tasks", {"view": "stats", "days": 0}, rs)
    assert "1..365" in bid, bid
    schema = next(t for t in m.TOOLS if t["name"] == "hub_tasks")["inputSchema"]
    assert schema["properties"]["view"]["enum"] == ["board", "stats"], schema
    assert schema["properties"]["days"]["type"] == "integer", schema


def t_team_digest_surface(hub):
    """R55 hub_status view=digest：流动/风险/建议/记分卡一张单，坏参与默认视图受控。"""
    rs = str(hub)
    empty = m.call_tool("hub_status", {"view": "digest"}, rs)
    assert "团队日结" in empty and "风险：无" in empty and "建议：" in empty, empty[:500]

    now = datetime.datetime.now()
    iso = lambda h: (now - datetime.timedelta(hours=h)).isoformat(timespec="seconds")
    day = lambda d: (now.date() + datetime.timedelta(days=d)).isoformat()
    m.call_tool("hub_heartbeat", {"agent": "zcode", "project": "测试-项目"}, rs)

    a = m.call_tool("hub_task_add", {"title": "日结完成单", "project": "测试-项目"}, rs)
    tid = int(a.split("#")[1].split("：")[0].split()[0])
    m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)
    with brain.db_conn(rs) as conn:
        conn.execute("UPDATE tasks SET created=?, claimed_at=? WHERE id=?",
                     (iso(10), iso(9), tid))
    assert "完成" in m.call_tool("hub_task_update", {
        "task_id": tid, "status": "done", "agent": "zcode"}, rs)

    b = m.call_tool("hub_task_add", {"title": "日结在办单", "project": "测试-项目"}, rs)
    tid2 = int(b.split("#")[1].split("：")[0].split()[0])
    m.call_tool("hub_claim", {"task_id": tid2, "agent": "zcode"}, rs)
    c = m.call_tool("hub_task_add", {"title": "日结无主单", "project": "测试-项目"}, rs)
    tid3 = int(c.split("#")[1].split("：")[0].split()[0])
    d = m.call_tool("hub_task_add", {"title": "日结死信单", "project": "测试-项目"}, rs)
    tid4 = int(d.split("#")[1].split("：")[0].split()[0])
    with brain.db_conn(rs) as conn:
        conn.execute("UPDATE tasks SET claimed_at=?, due=?, approval_required=1, approved_by='' "
                     "WHERE id=?", (iso(3), day(-1), tid2))
        conn.execute("UPDATE tasks SET status='in_progress', owner='', claimed_at='', "
                     "updated=? WHERE id=?", (iso(1), tid3))
        conn.execute("UPDATE tasks SET status='dead', owner='zcode', attempts=2, "
                     "last_error='boom', updated=? WHERE id=?", (iso(1), tid4))
        conn.execute("INSERT INTO tool_calls(ts,agent,tool) VALUES(?, 'zcode', 'hub_status')",
                     (iso(1),))
        conn.execute("INSERT INTO searches(ts,agent,tool,query,hits)"
                     " VALUES(?, 'zcode','hub_search','不存在',0)", (iso(1),))
    brain.error_add(rs, "zcode", "未闭错误", "detail", "")

    out = m.call_tool("hub_status", {"view": "digest", "days": 1}, rs)
    for needle in ("团队日结", "流动：", "当前：在线", "租约逾期", "无主在办", "等审批",
                   "逾期", "死信", "未闭错误", "零命中检索", "建议：", "按 agent",
                   "zcode", "风险："):
        assert needle in out, (needle, out[:800])
    assert "→" in out, out[:800]
    bad = m.call_tool("hub_status", {"view": "nope"}, rs)
    assert "错误" in bad and "now/digest" in bad, bad
    bid = m.call_tool("hub_status", {"view": "digest", "days": 0}, rs)
    assert "1..365" in bid, bid
    bint = m.call_tool("hub_status", {"view": "digest", "days": "abc"}, rs)
    assert "整数" in bint, bint
    assert "大脑此刻状态" in m.call_tool("hub_status", {}, rs)
    schema = next(t for t in m.TOOLS if t["name"] == "hub_status")["inputSchema"]
    assert schema["properties"]["view"]["enum"] == ["now", "digest"], schema
    assert schema["properties"]["days"]["type"] == "integer", schema


def t_wip_limit_surface(hub):
    """R38：WIP 到顶时 hub_claim 拒收、列表显示在办/上限、完成后可再领。"""
    rs = str(hub)
    old = m.brain.MAX_WIP_PER_AGENT
    m.brain.MAX_WIP_PER_AGENT = 1
    try:
        m.call_tool("hub_heartbeat", {"agent": "zcode", "project": "测试-项目"}, rs)
        add1 = m.call_tool("hub_task_add", {"title": "WIP一", "project": "测试-项目"}, rs)
        tid1 = int(add1.split("#")[1].split("：")[0].split()[0])
        add2 = m.call_tool("hub_task_add", {"title": "WIP二", "project": "测试-项目"}, rs)
        tid2 = int(add2.split("#")[1].split("：")[0].split()[0])
        assert "认领成功" in m.call_tool("hub_claim", {"task_id": tid1, "agent": "zcode"}, rs)
        denied = m.call_tool("hub_claim", {"task_id": tid2, "agent": "zcode"}, rs)
        assert "WIP 上限 1" in denied, denied
        listed = m.call_tool("hub_list_agents", {}, rs)
        assert "在办 1/1" in listed, listed
        assert "已更新" in m.call_tool("hub_task_update", {
            "task_id": tid1, "status": "done", "agent": "zcode"}, rs)
        assert "认领成功" in m.call_tool("hub_claim", {"task_id": tid2, "agent": "zcode"}, rs)
    finally:
        m.brain.MAX_WIP_PER_AGENT = old


def t_task_cancel_surface(hub):
    """R41：取消必须带原因；回执含终态说明；看板可按已取消过滤；前沿不再出现。"""
    rs = str(hub)
    add = m.call_tool("hub_task_add", {"title": "取消面任务", "project": "测试-项目"}, rs)
    tid = int(add.split("#")[1].split("：")[0].split()[0])
    assert "认领成功" in m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)
    bad = m.call_tool("hub_task_update", {
        "task_id": tid, "status": "cancelled", "agent": "zcode"}, rs)
    assert "cancel_reason" in bad, bad
    ok = m.call_tool("hub_task_update", {
        "task_id": tid, "status": "cancelled", "agent": "zcode",
        "cancel_reason": "需求取消"}, rs)
    assert "已取消" in ok and "取消原因：需求取消" in ok and "终态" in ok, ok
    listed = m.call_tool("hub_tasks", {"status": "cancelled"}, rs)
    assert "取消面任务" in listed and "取消：需求取消" in listed, listed
    nxt = m.call_tool("hub_next", {"project": "测试-项目", "agent": "zcode"}, rs)
    assert "取消面任务" not in nxt, nxt
    schema = next(t for t in m.TOOLS if t["name"] == "hub_task_update")["inputSchema"]
    assert schema["properties"]["cancel_reason"]["type"] == "string", schema
    assert "cancel_reason" not in (schema.get("required") or []), schema


def t_task_retry_surface(hub):
    """R42/R43：退避期认领被拒、看板/前沿标注、delay=0 提前放行、
    死信退出队列且需 retry 复活；schema 必填 reason。"""
    rs = str(hub)
    old_base, old_max = m.brain.RETRY_BASE_SECONDS, m.brain.MAX_TASK_ATTEMPTS
    m.brain.RETRY_BASE_SECONDS, m.brain.MAX_TASK_ATTEMPTS = 60, 3
    try:
        add = m.call_tool("hub_task_add", {"title": "重试面任务", "project": "测试-项目"}, rs)
        tid = int(add.split("#")[1].split("：")[0].split()[0])
        assert "认领成功" in m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)
        bad = m.call_tool("hub_task_retry", {"task_id": tid, "agent": "zcode"}, rs)
        assert "reason" in bad, bad
        first = m.call_tool("hub_task_retry", {"task_id": tid, "agent": "zcode",
                                               "reason": "接口 502"}, rs)
        assert "已重试排队" in first and "退避" in first and "第 1 次失败" in first, first
        denied = m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)
        assert "退避" in denied, denied
        listed = m.call_tool("hub_tasks", {"status": "pending"}, rs)
        assert "↻退避" in listed and "重试面任务" in listed, listed
        nxt = m.call_tool("hub_next", {"project": "测试-项目", "agent": "zcode"}, rs)
        assert "退避中 1 条" in nxt and "重试面任务" in nxt, nxt
        early = m.call_tool("hub_task_retry", {"task_id": tid, "agent": "zcode",
                                               "reason": "已修复", "delay": 0}, rs)
        assert "立即可认领" in early and "第 2 次失败" in early, early
        assert "认领成功" in m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)
        dead = m.call_tool("hub_task_retry", {"task_id": tid, "agent": "zcode",
                                              "reason": "第三次失败"}, rs)
        assert "死信" in dead and "hub_task_retry" in dead, dead
        denied2 = m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)
        assert "死信" in denied2, denied2
        listed2 = m.call_tool("hub_tasks", {"status": "dead"}, rs)
        assert "重试面任务" in listed2 and "☠死信" in listed2, listed2
        revived = m.call_tool("hub_task_retry", {"task_id": tid, "agent": "qoder",
                                                 "reason": "人工复活", "delay": 0}, rs)
        assert "已重试排队" in revived and "第 1 次失败" in revived, revived
    finally:
        m.brain.RETRY_BASE_SECONDS, m.brain.MAX_TASK_ATTEMPTS = old_base, old_max
    schema = next(t for t in m.TOOLS if t["name"] == "hub_task_retry")["inputSchema"]
    assert schema["properties"]["delay"]["type"] == "integer", schema
    assert set(schema.get("required") or []) == {"task_id", "reason"}, schema


def t_task_defer_approve_surface(hub):
    """R44/R45：hub_task_update(defer_until) 延后回执/看板标注/退出前沿/唤醒；
    approval_required 任务 done 前被拦，hub_task_approve 四眼批准后放行；schema 可选。"""
    rs = str(hub)
    m.call_tool("hub_agent", {"action": "skills", "agent": "zcode",
                              "skills": "python", "confirm": True}, rs)
    m.call_tool("hub_agent", {"action": "skills", "agent": "workbuddy",
                              "skills": "python", "confirm": True}, rs)

    # ---- R44 ----
    add = m.call_tool("hub_task_add", {"title": "延后面任务", "project": "测试-项目"}, rs)
    tid = int(add.split("#")[1].split("：")[0].split()[0])
    bad = m.call_tool("hub_task_update", {"task_id": tid, "defer_until": "2000-01-01",
                                          "defer_reason": "过去"}, rs)
    assert "未来" in bad, bad
    bad2 = m.call_tool("hub_task_update", {"task_id": tid, "defer_until": "2030-01-01"}, rs)
    assert "reason" in bad2, bad2
    ok = m.call_tool("hub_task_update", {"task_id": tid, "agent": "zcode",
                                         "defer_until": "2030-01-01",
                                         "defer_reason": "等上游"}, rs)
    assert "已延后至" in ok and "唤醒" in ok, ok
    listed = m.call_tool("hub_tasks", {"status": "pending"}, rs)
    assert "⏸延后" in listed and "延后面任务" in listed, listed
    nxt = m.call_tool("hub_next", {"project": "测试-项目", "agent": "zcode"}, rs)
    assert "延后中 1 条" in nxt and "延后面任务" in nxt, nxt
    denied = m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)
    assert "延后" in denied, denied
    wake = m.call_tool("hub_task_update", {"task_id": tid, "agent": "zcode",
                                           "defer_until": "-"}, rs)
    assert "已恢复" in wake, wake
    wake2 = m.call_tool("hub_task_update", {"task_id": tid, "agent": "zcode",
                                            "defer_until": "-"}, rs)
    assert "没有延后标记" in wake2, wake2
    assert "认领成功" in m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)

    # ---- R45 ----
    add2 = m.call_tool("hub_task_add", {"title": "审批面任务", "project": "测试-项目",
                                        "approval_required": True}, rs)
    tid2 = int(add2.split("#")[1].split("：")[0].split()[0])
    assert "认领成功" in m.call_tool("hub_claim", {"task_id": tid2, "agent": "zcode"}, rs)
    blocked = m.call_tool("hub_task_update", {
        "task_id": tid2, "status": "done", "agent": "zcode"}, rs)
    assert "要求完成审批" in blocked and "hub_task_approve" in blocked, blocked
    selfapprove = m.call_tool("hub_task_approve", {"task_id": tid2, "agent": "zcode"}, rs)
    assert "不能给自己" in selfapprove, selfapprove
    approved = m.call_tool("hub_task_approve", {"task_id": tid2, "agent": "workbuddy",
                                                "note": "验收通过"}, rs)
    assert "已批准完成" in approved and "workbuddy" in approved, approved
    listed2 = m.call_tool("hub_tasks", {"status": "in_progress"}, rs)
    assert "✓已审批（workbuddy）" in listed2, listed2
    done = m.call_tool("hub_task_update", {
        "task_id": tid2, "status": "done", "agent": "zcode"}, rs)
    assert "已完成" in done or "已更新" in done, done
    done2 = m.call_tool("hub_task_approve", {"task_id": tid2, "agent": "workbuddy"}, rs)
    assert "终态" in done2, done2

    add3 = m.call_tool("hub_task_add", {"title": "普通面任务", "project": "测试-项目"}, rs)
    tid3 = int(add3.split("#")[1].split("：")[0].split()[0])
    notneeded = m.call_tool("hub_task_approve", {"task_id": tid3, "agent": "workbuddy"}, rs)
    assert "未开启" in notneeded, notneeded

    schema = next(t for t in m.TOOLS if t["name"] == "hub_task_approve")["inputSchema"]
    assert schema["properties"]["note"]["type"] == "string", schema
    assert set(schema.get("required") or []) == {"task_id"}, schema
    upd = next(t for t in m.TOOLS if t["name"] == "hub_task_update")["inputSchema"]
    assert upd["properties"]["defer_until"]["type"] == "string", upd
    assert upd["properties"]["approval_required"]["type"] == "boolean", upd
    add_schema = next(t for t in m.TOOLS if t["name"] == "hub_task_add")["inputSchema"]
    assert add_schema["properties"]["approval_required"]["type"] == "boolean", add_schema


def t_freeze_surface(hub):
    """R48 全局写入冻结：reason/分钟范围校验；冻结后写工具全拒、读工具与状态查询照常；
    hub_freeze/hub_undo 免冻结；延长/过期自动失效/解冻幂等；冻结动作进审计链。"""
    rs = str(hub)
    m.call_tool("hub_agent", {"action": "skills", "agent": "zcode",
                              "skills": "python", "confirm": True}, rs)
    st = m.call_tool("hub_freeze", {"action": "status"}, rs)
    assert "未冻结" in st, st
    bad = m.call_tool("hub_freeze", {"action": "freeze", "minutes": 30}, rs)
    assert "reason" in bad, bad
    bad = m.call_tool("hub_freeze", {"action": "freeze", "minutes": 1441, "reason": "太长"}, rs)
    assert "minutes" in bad, bad
    bad = m.call_tool("hub_freeze", {"action": "nope"}, rs)
    assert "action" in bad, bad

    # 冻结前留一条可撤销记录，供 hub_undo 免冻结验证
    assert "已记入大脑" in m.call_tool("hub_log_work", {
        "project": "测试-项目", "agent": "zcode",
        "content": "【目的】冻结测试\n【做了什么】写一条待撤销记录\n【验证结果】待验证\n【如何回滚】hub_undo"},
        rs)

    out = m.call_tool("hub_freeze", {"action": "freeze", "minutes": 30,
                                     "reason": "迁移中避免并发写", "agent": "zcode"}, rs)
    assert "已冻结至" in out and "30" in out and "zcode" in out, out

    blocked = m.call_tool("hub_task_add", {"title": "冻结期间不该写入"}, rs)
    assert "冻结" in blocked and "unfreeze" in blocked, blocked
    read = m.call_tool("hub_tasks", {}, rs)
    assert "冻结" not in read, read[:200]  # 读工具不受影响
    status = m.call_tool("hub_status", {}, rs)
    assert "全局写入已冻结" in status and "迁移中" in status, status[:400]
    assert "读工具照常" in m.call_tool("hub_freeze", {"action": "status"}, rs)

    # hub_undo 免冻结：解冻/修复不能自锁
    undo = m.call_tool("hub_undo", {"agent": "zcode"}, rs)
    assert "已撤销" in undo and "已全局冻结" not in undo, undo

    ext = m.call_tool("hub_freeze", {"action": "freeze", "minutes": 0,
                                     "reason": "延长为手动解冻", "agent": "zcode"}, rs)
    assert "已延长" in ext and "手动解冻" in ext and "已延长至" in ext, ext

    # 到期自动失效：改 until 到过去，状态报过期且写入恢复
    import json as _json
    with brain.db_conn(rs) as conn:
        d = _json.loads(conn.execute(
            "SELECT value FROM meta WHERE key='write_freeze'").fetchone()[0])
        d["until"] = "2000-01-01T00:00:00"
        conn.execute("UPDATE meta SET value=? WHERE key='write_freeze'",
                     (_json.dumps(d, ensure_ascii=False),))
    assert "已过期" in m.call_tool("hub_freeze", {"action": "status"}, rs)
    add = m.call_tool("hub_task_add", {"title": "过期后恢复写入"}, rs)
    assert "冻结" not in add and "#" in add, add

    # 解冻幂等 + 审计链完整
    m.call_tool("hub_freeze", {"action": "freeze", "minutes": 0, "reason": "再冻一次"}, rs)
    off = m.call_tool("hub_freeze", {"action": "unfreeze", "agent": "zcode"}, rs)
    assert "已解除" in off, off
    off2 = m.call_tool("hub_freeze", {"action": "unfreeze", "agent": "zcode"}, rs)
    assert "没有生效" in off2, off2

    schema = next(t for t in m.TOOLS if t["name"] == "hub_freeze")["inputSchema"]
    assert set(schema["properties"]["action"]["enum"]) == {"status", "freeze", "unfreeze"}, schema
    assert schema["properties"]["minutes"]["type"] == "integer", schema
    assert "hub_freeze" in m.WRITE_TOOLS and m.FREEZE_EXEMPT == {"hub_freeze", "hub_undo"}
    assert brain.journal_verify(rs)["ok"], brain.journal_verify(rs)
    audit = m.call_tool("hub_audit", {"limit": 5}, rs)
    assert "审计链" in audit, audit


def t_audit_export_surface(hub):
    """R50 审计导出：JSONL 首行 meta 带链快照；journal+op_audit 均在场；CSV 表头；
    坏参拒收；链异常照导但明确告警；空库导出可读。"""
    rs = str(hub)
    m.call_tool("hub_agent", {"action": "skills", "agent": "zcode",
                              "skills": "python", "confirm": True}, rs)
    m.call_tool("hub_log_work", {"project": "审计导出", "agent": "zcode",
                                 "content": "【目的】导出\n【做了什么】写一条\n"
                                            "【验证结果】ok\n【如何回滚】undo"}, rs)
    # 直接调 call_tool 不走 handle_message 的写审计钩子，这里补一条 op_audit 覆盖两类流水
    brain.audit_add(rs, "zcode", "hub_log_work", "审计导出", "测试审计行", "record", "1")

    out = m.call_tool("hub_export", {"action": "audit"}, rs)
    assert "已导出审计流水" in out and "JSONL" in out and "审计链" in out, out
    files = sorted((Path(rs) / "_hub" / "exports").glob("audit-*.jsonl"))
    assert files, out
    lines = files[-1].read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])
    assert meta["type"] == "meta" and meta["chain"]["ok"] is True, meta
    assert meta["journal"] == len(lines) - 1 - meta["op_audit"], meta
    rows = [json.loads(ln) for ln in lines[1:]]
    types = {r["type"] for r in rows}
    assert "journal" in types and "op_audit" in types, types
    assert any(r.get("hash") for r in rows if r["type"] == "journal"), rows[:3]

    csv_out = m.call_tool("hub_export", {"action": "audit", "audit_format": "csv",
                                         "days": 0}, rs)
    assert "CSV" in csv_out and "全部历史" in csv_out, csv_out
    cfiles = sorted((Path(rs) / "_hub" / "exports").glob("audit-*.csv"))
    assert cfiles, csv_out
    text = cfiles[-1].read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].startswith("source,id,ts,agent,action,project"), text[:120]
    assert "journal," in text and "op_audit," in text, text[:300]

    bad = m.call_tool("hub_export", {"action": "audit", "audit_format": "xml"}, rs)
    assert "jsonl/csv" in bad, bad
    bad = m.call_tool("hub_export", {"action": "audit", "days": -1}, rs)
    assert "days" in bad, bad
    trunc = m.call_tool("hub_export", {"action": "audit", "limit": 1, "days": 0}, rs)
    assert "截断" in trunc, trunc

    # 链异常照导，但回执必须告警（出事时最需要现场）
    with brain.db_conn(rs) as conn:
        jid = conn.execute("SELECT id FROM journal WHERE hash!='' "
                           "ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.execute("UPDATE journal SET note='被改' WHERE id=?", (jid,))
    broken = m.call_tool("hub_export", {"action": "audit", "days": 0}, rs)
    assert "已导出" in broken and "审计链异常" in broken and "勿重算" in broken, broken

    schema = next(t for t in m.TOOLS if t["name"] == "hub_export")["inputSchema"]
    assert "audit" in schema["properties"]["action"]["enum"], schema
    assert set(schema["properties"]["audit_format"]["enum"]) == {"jsonl", "csv"}, schema
    assert schema["properties"]["days"]["type"] == "integer", schema

    # 空库：meta 只有一行，链为空态通过
    empty = fresh_hub(Path(hub).parent, "hub_audit_empty")
    m.call_tool("hub_export", {"action": "audit", "days": 0}, str(empty))
    efiles = sorted((Path(empty) / "_hub" / "exports").glob("audit-*.jsonl"))
    elines = efiles[-1].read_text(encoding="utf-8").splitlines()
    emeta = json.loads(elines[0])
    assert len(elines) == 1 and emeta["chain"]["ok"] and emeta["journal"] == 0, emeta


def t_project_ref_surface(hub):
    """R51 写入口项目名护栏：穿越/盘符/保留目录拒收且不落库；合法中文名照常。"""
    rs = str(hub)
    m.call_tool("hub_agent", {"action": "skills", "agent": "zcode",
                              "skills": "python", "confirm": True}, rs)
    with brain.db_conn(rs) as conn:
        n0 = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    content = ("【目的】护栏\n【做了什么】尝试写入\n【验证结果】待验证\n【如何回滚】undo")
    for bad_name in ("../逃逸", "..\\逃逸", "C:\\temp", "a/b", "_hub"):
        out = m.call_tool("hub_log_work", {"project": bad_name, "agent": "zcode",
                                           "content": content}, rs)
        assert "错误" in out and "project" in out, (bad_name, out)
    with brain.db_conn(rs) as conn:
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == n0
    assert "错误" in m.call_tool("hub_task_add", {"title": "穿越", "project": "../x"}, rs)
    assert "错误" in m.call_tool("hub_task_add", {"title": "保留", "project": "_hub"}, rs)
    assert "错误" in m.call_tool("hub_create_project", {"project": "../逃逸"}, rs)
    assert not (Path(rs).parent / "逃逸").exists()
    assert "错误" in m.call_tool("hub_archive_project", {"project": "../x"}, rs)
    assert "错误" in m.call_tool("hub_archive_project", {"project": "../x", "restore": True}, rs)

    ok = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "zcode",
                                      "content": content}, rs)
    assert "已记入大脑" in ok, ok
    assert "#" in m.call_tool("hub_task_add", {"title": "正常", "project": "测试-项目"}, rs)


def t_closure_quality_surface(hub):
    """R40：done 回执带质量提示；hub_task_quality 单查/巡检可用；schema 为可选参数。"""
    rs = str(hub)
    add = m.call_tool("hub_task_add", {"title": "质量面任务", "project": "测试-项目"}, rs)
    tid = int(add.split("#")[1].split("：")[0].split()[0])
    assert "认领成功" in m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)
    done = m.call_tool("hub_task_update", {
        "task_id": tid, "status": "done", "agent": "zcode"}, rs)
    assert "完成质量" in done and "条提示" in done, done
    one = m.call_tool("hub_task_quality", {"task_id": tid}, rs)
    assert "完成质量" in one and "警告：" in one and "统计：" in one, one
    assert "只有认领自动锚点" in one, one
    report = m.call_tool("hub_task_quality", {"days": 1}, rs)
    assert "完成质量巡检" in report and f"#{tid}" in report, report
    bad = m.call_tool("hub_task_quality", {"task_id": 99999}, rs)
    assert "不存在" in bad, bad
    schema = next(t for t in m.TOOLS if t["name"] == "hub_task_quality")["inputSchema"]
    assert schema["properties"]["task_id"]["type"] == "integer", schema
    assert not (schema.get("required") or []), schema


def t_task_handoff_surface(hub):
    """R39：hub_task_update(handoff_to) 交办成功/非持有者拒收/schema 可选参数。"""
    rs = str(hub)
    m.call_tool("hub_agent", {"action": "skills", "agent": "zcode",
                              "skills": "python", "confirm": True}, rs)
    m.call_tool("hub_agent", {"action": "skills", "agent": "workbuddy",
                              "skills": "python", "confirm": True}, rs)
    add = m.call_tool("hub_task_add", {"title": "交办任务", "project": "测试-项目",
                                       "skills": "python"}, rs)
    tid = int(add.split("#")[1].split("：")[0].split()[0])
    assert "认领成功" in m.call_tool("hub_claim", {"task_id": tid, "agent": "zcode"}, rs)
    denied = m.call_tool("hub_task_update", {"task_id": tid, "agent": "qoder",
                                             "handoff_to": "workbuddy"}, rs)
    assert "只有持有者" in denied, denied
    ok = m.call_tool("hub_task_update", {"task_id": tid, "agent": "zcode",
                                         "handoff_to": "workbuddy",
                                         "handoff_note": "剩下联调"}, rs)
    assert "已交办" in ok and "zcode→workbuddy" in ok, ok
    assert m.brain.task_get(rs, tid)["owner"] == "workbuddy"
    schema = next(t for t in m.TOOLS if t["name"] == "hub_task_update")["inputSchema"]
    assert schema["properties"]["handoff_to"]["type"] == "string", schema
    assert "handoff_to" not in (schema.get("required") or []), schema


def t_db_corrupt_protocol(tmp):
    """R59：协议层遇库文件损坏——isError 给恢复路径，且不再谎称「已记入大脑错误」
    （库都读不了，error_add 必然失败；假成功比报错更危险）。"""
    hub = Path(tmp) / "hub_corrupt"
    (hub / core.DIR_META).mkdir(parents=True)
    (hub / core.DIR_META / brain.DB_NAME).write_bytes(b"garbage" * 4096)
    r = resp_ok(m.handle_message(
        rpc("tools/call", {"name": "hub_status", "arguments": {"agent": "zcode"}}), str(hub)))
    assert r.get("isError") is True, r
    text = r["content"][0]["text"]
    for needle in ("数据库文件已损坏", "brain.db", "-wal", "§3.4"):
        assert needle in text, (needle, text[:400])
    assert "已记入大脑错误" not in text, text[:400]
    assert (r.get("structuredContent") or {}).get("db_corrupt") is True, r


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_mcp_")
    root = Path(tmp) / "hub"
    build_hub(root)
    print(f"临时目录：{tmp}\n")
    case("交接协议工具（create/list/claim/重复认领拒绝/空态）", lambda: t_handoff_tool(root))
    case("MCP协议（握手/78工具/未知方法/异常自动登记/ping）", lambda: t_protocol(root))
    case("R4·初始化说明渐进披露（instructions 非空/核心流程覆盖/无幽灵工具名/单段预算内）",
         lambda: t_initialize_instructions(root))
    case("isError 正确性（成功=False / 缺参·未知工具·claim缺agent=True）", lambda: t_iserror_correctness(root))
    case("R59·库损坏协议降级（isError+恢复路径+db_corrupt，不谎称已登记）",
         lambda: t_db_corrupt_protocol(tmp))
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
    case("R11 文件租约工具面（查占/获取/冲突/续租/释放/list/dry_run/心跳预警与续租/看板锁计数）",
         lambda: t_file_lease_surface(fresh_hub(tmp, "hublock")))
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
    case("项目归档/恢复工具（状态+目录/未找到/重复归档/restore 往返）", lambda: t_archive_tool(tmp))
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
    case("R9·未知工具可执行诊断（拼错最近名/MCP全名前缀/无匹配留发现入口/零命中检索建议）",
         lambda: t_unknown_tool_diagnostics(root))
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
    case("D1·写工具幂等键（重试原样重放不重复记/同键异内容拒收/agent 命名空间/过期不重放/占位不重复执行/超时接管/memory+note 同效）",
         lambda: t_idem_key(fresh_hub(tmp, "hub_idem")))
    case("D4·tools/list 缓存提示+确定性排序（2026-07-28 SEP-2549：resultType/ttlMs≥0/private/逐字节一致/切片保序/env 可调）",
         lambda: t_list_cache_hints(fresh_hub(tmp, "hub_list")))
    case("D4·JSON Schema 方言安全守卫（不锁 $schema / 无 2020-12 特有关键字 + 反向验证）",
         lambda: t_schema_dialect_safety(root))
    case("D6·使用留痕全覆盖（写工具/原留痕内/原留痕外三类都留/失败不留/fail-open 不反噬业务）",
         lambda: t_usage_trace_completeness(fresh_hub(tmp, "hub_trace")))
    case("R28·调用归属（锁定身份优先/clientInfo兜底/筛选对象不冒充调用者/无线索记空）",
         lambda: t_call_attribution(fresh_hub(tmp, "hub_attr")))
    case("D8·错误分类学三桶（permanent不诱导重试/transient提示退避/ambiguous兜底+幂等键/error_kind机器可读）",
         lambda: t_error_taxonomy(root))
    case("D5出口·hub_memory_supersede（裁决后退出检索/数据保留/j撤销/坏参拒/过闸门/留痕/总数不变）",
         lambda: t_memory_supersede_exit(fresh_hub(tmp, "hub_supersede")))
    case("v2.40.76·记录侧取代出口（target=record 退出检索/正文可追溯+标注/反向撤销/journal/schema枚举）",
         lambda: t_record_supersede_surface(fresh_hub(tmp, "hub_rec_supersede")))
    case("D10·hub_usage_timeline 工具面（worker切片/只读契约/两视图/未知view回落/整数守卫/过滤透传/只读不改数）",
         lambda: t_usage_timeline_surface(fresh_hub(tmp, "hub_usage_surface")))
    case("R3·多版本处置可见性（ghost对旧版行标注现行版/近重复折叠留新版+回溯/行格式不破）",
         lambda: t_ghost_demotion_surface(fresh_hub(tmp, "hub_demote_surface")))
    case("R4·新鲜度标注可见性（stale行提示复核/age过期行标历史证据/fresh不误标/valid_until契约/行格式不破）",
         lambda: t_freshness_surface(fresh_hub(tmp, "hub_fresh_surface")))
    case("R5·hub_memory_confirm 工具面（参数闸门/ids路径/批量kind+limit+remaining/硬过期跳过/journal/总数不变）",
         lambda: t_memory_confirm_tool(fresh_hub(tmp, "hub_confirm_surface")))
    case("R5·agent 注册表准入（写工具无死枚举/登记即可写/未在册仍拒+名单/permanent）",
         lambda: t_agent_registry_surface(fresh_hub(tmp, "hub_registry_surface")))
    case("R6/R7·接入状态机（未注册也出清单/plan只列缺口/verify落快照/快照过期/运行版本告警/坏action拒）",
         lambda: t_agent_onboard_surface(fresh_hub(tmp, "hub_onboard_surface")))
    case("R7·Agent 控制面持有任务（列表在办+租约/空闲无噪音/单agent状态追加/无主点名）",
         lambda: t_agent_holdings_surface(fresh_hub(tmp, "hub_holdings_surface")))
    case("R8·任务看板可见性（hub_tasks截止·前置·阻塞·租约 / hub_next逾期·前置缺失·可接管）",
         lambda: t_task_board_surface(fresh_hub(tmp, "hub_board_surface")))
    case("R7·备份清理工具面（backups 盘点/预演不删/apply 才删并留 journal）",
         lambda: t_backup_prune_surface(tmp))
    case("R61·恢复演练工具面（默认最新整包/现场零改动/差异告警/坏包拒绝/无备份提示）",
         lambda: t_backup_drill_surface(tmp))
    case("R8·一键自检工具面（action=report 出报告+修复建议/不广播信号）",
         lambda: t_sentry_report(tmp))
    case("R9·场景帮助与运维手册（六主题/未知主题列可选/文档覆盖恢复·发布·任务治理）",
         lambda: t_help_topics(tmp))
    case("R28·新项目拼写提示（首次出现+相近项目/已登记静默）",
         lambda: t_new_project_hint(fresh_hub(tmp, "hub_new_project")))
    case("R35·log_work 任务绑定（落库/项目一致/完成声明告警/续租/状态页漂移/schema可选）",
         lambda: t_log_work_task_binding(fresh_hub(tmp, "hub_task_binding")))
    case("R36·agent 管控面（读/切状态/替他人confirm/认领闸门/前沿告警/列表徽标/schema）",
         lambda: t_agent_control_surface(fresh_hub(tmp, "hub_agent_control")))
    case("R37·能力路由工具面（技能读设清/任务skills/匹配认领/不匹配拒/前沿过滤/schema）",
         lambda: t_skill_routing_surface(fresh_hub(tmp, "hub_skill_routing")))
    case("R38·WIP 上限工具面（到顶拒收/列表显示/完成释放）",
         lambda: t_wip_limit_surface(fresh_hub(tmp, "hub_wip_limit")))
    case("R39·任务交办工具面（handoff_to 成功/非持有者拒收/schema可选）",
         lambda: t_task_handoff_surface(fresh_hub(tmp, "hub_task_handoff")))
    case("R40·完成质量工具面（done 回执提示/单查/巡检/schema可选）",
         lambda: t_closure_quality_surface(fresh_hub(tmp, "hub_task_quality")))
    case("R41·任务取消工具面（原因必填/回执终态/看板过滤/前沿消失/schema可选）",
         lambda: t_task_cancel_surface(fresh_hub(tmp, "hub_task_cancel")))
    case("R42/R43·失败重试与死信工具面（退避拒认领/看板前沿/提前放行/死信复活/schema）",
         lambda: t_task_retry_surface(fresh_hub(tmp, "hub_task_retry")))
    case("R44/R45·延后与完成审批工具面（延后回执/看板前沿/唤醒/审批拦截/四眼批准/schema）",
         lambda: t_task_defer_approve_surface(fresh_hub(tmp, "hub_task_defer_approve")))
    case("R48·全局写入冻结工具面（原因/分钟校验/写拒读通/状态与解冻豁免/延长/过期恢复/审计链）",
         lambda: t_freeze_surface(fresh_hub(tmp, "hub_freeze")))
    case("R50·审计导出（JSONL meta 链快照/两类流水在场/CSV 表头/坏参/截断/链异常照导/空库）",
         lambda: t_audit_export_surface(fresh_hub(tmp, "hub_audit_export")))
    case("R51·项目名护栏（穿越/盘符/保留目录拒收不落库/归档恢复同护栏/合法名照常）",
         lambda: t_project_ref_surface(fresh_hub(tmp, "hub_project_ref")))
    case("R53·任务过程统计（流量/周期/在办卡点/重试/风险/空项目/坏参/schema）",
         lambda: t_task_stats_surface(fresh_hub(tmp, "hub_task_stats")))
    case("R55 hub_status view=digest 团队日结（流动/风险/建议/记分卡，坏参与默认视图）",
         lambda: t_team_digest_surface(fresh_hub(tmp, "hub_team_digest")))

    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("MCP ALL PASS")


if __name__ == "__main__":
    main()
