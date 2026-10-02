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
    assert r["serverInfo"]["version"] == "2.0.0"
    assert m.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, str(root)) is None
    tools = resp_ok(m.handle_message(rpc("tools/list"), str(root)))["tools"]
    names = {t["name"] for t in tools}
    assert {"hub_list_projects", "hub_log_work", "hub_memory_read", "hub_memory_write",
            "hub_get_progress", "hub_list_skills", "hub_list_mcps", "hub_search",
            "hub_get_project", "hub_create_project", "hub_get_rules", "hub_heartbeat",
            "hub_report_error", "hub_list_errors", "hub_undo", "hub_list_agents",
            "hub_list_todos", "hub_health", "hub_todo_done", "hub_env_set", "hub_env_list", "hub_search_files"} <= names, names
    assert len(names) == 22
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
    assert "[记录]" in out
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
    out = m.call_tool("hub_log_work", {"project": "", "agent": "x", "content": " "}, root_s)
    assert "必填" in out
    # project 只作为 DB 文本行落库，无文件系统操作（穿越由 create_project 校验拦截）
    out = m.call_tool("hub_log_work", {"project": "..\\逃逸", "agent": "x", "content": "y"}, root_s)
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
    assert len(brain.search_memories(root, "全新的记忆")) == 1
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


def t_concurrent_log_work(tmp):
    root = Path(tmp) / "hub"
    root_s = str(root)
    import threading
    errs = []

    def worker(i):
        out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": f"并发{i}",
                                           "content": f"第{i}号并发记录内容", "date": "2026-09-30"}, root_s)
        if "已记入大脑" not in out:
            errs.append(out)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    recs = brain.list_records(root_s, project="测试-项目", limit=2000)
    for i in range(8):
        assert any(f"（并发{i}）" in r["title"] and f"第{i}号并发记录内容" in r["content"] for r in recs), \
            f"并发第{i}条丢失"
    jl = brain.journal_list(root_s, 1000)
    log_entries = [e for e in jl if e.get("action") == "log_work"
                   and str(e.get("agent", "")).startswith("并发")]
    assert len(log_entries) == 8, f"journal 应有 8 条并发记录，实际 {len(log_entries)}"


def t_heartbeat_conflict(root):
    root = str(root)
    out = m.call_tool("hub_heartbeat", {"agent": "alpha", "project": "测试-项目", "note": "改UI"}, root)
    assert "无其他活跃会话" in out
    out = m.call_tool("hub_heartbeat", {"agent": "beta", "project": "测试-项目", "note": "改驱动"}, root)
    assert "撞车预警" in out and "alpha" in out, out
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "alpha",
                                       "content": "alpha 的活干完了"}, root)
    assert "撞车预警" in out, out
    out = m.call_tool("hub_heartbeat", {"agent": "beta", "project": "别的-项目"}, root)
    assert "无其他活跃会话" in out
    # 陈旧清理：手改 ts
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE sessions SET ts='2026-09-30T08:00:00' WHERE agent='alpha'")
    out = m.call_tool("hub_heartbeat", {"agent": "beta", "project": "测试-项目"}, root)
    assert "alpha" not in out, "陈旧会话未被清理"
    with brain.db_conn(root) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE agent='alpha'").fetchone()[0] == 0
    # agent 必填
    out = m.call_tool("hub_heartbeat", {"agent": "  "}, root)
    assert "必填" in out


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
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "undoA",
                                       "content": "undoA 第一件事", "date": "2026-09-29"}, root_s)
    assert "已记入大脑" in out, out
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "undoA",
                                       "content": "undoA 第二件事写错了", "date": "2026-09-30"}, root_s)
    assert "已记入大脑" in out, out
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "undoB",
                                       "content": "undoB 的事不能被误伤", "date": "2026-09-30"}, root_s)
    assert "已记入大脑" in out, out
    # 撤 undoA 最新一条
    out = m.call_tool("hub_undo", {"agent": "undoA"}, root_s)
    assert "已撤销" in out, out
    rows = brain.list_records(root_s, project="测试-项目", limit=2000)
    assert all("第二件事写错" not in r["content"] for r in rows), "目标记录仍可见"
    assert any("undoA 第一件事" in r["content"] for r in rows), "误伤更早的记录"
    assert any("undoB 的事不能被误伤" in r["content"] for r in rows), "误伤他人"
    assert any("初始化记录" in r["content"] for r in rows), "误伤迁移记录"
    # 重复撤销 = 栈式撤上一条
    out = m.call_tool("hub_undo", {"agent": "undoA"}, root_s)
    assert "已撤销" in out, out
    rows = brain.list_records(root_s, project="测试-项目", limit=2000)
    assert all("undoA" not in r["agent"] for r in rows)
    assert any("undoB 的事不能被误伤" in r["content"] for r in rows)
    assert any("初始化记录" in r["content"] for r in rows)
    # 撤无可撤 / 无记录 agent / 必填
    out = m.call_tool("hub_undo", {"agent": "undoA"}, root_s)
    assert "没有找到" in out
    out = m.call_tool("hub_undo", {"agent": "没干活的"}, root_s)
    assert "没有找到" in out
    out = m.call_tool("hub_undo", {"agent": ""}, root_s)
    assert "必填" in out
    # 撤销操作进 DB 流水
    jl = brain.journal_list(root_s, 1000)
    assert len([e for e in jl if e.get("action") == "undo_log_work"]) == 2


def t_new_tools(tmp):
    root_s = str(Path(tmp) / "hub")
    # 参数健壮化（v2）：content 超长拒绝、date 畸形兜底、limit 容错
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "x",
                                       "content": "y" * (m.MAX_CONTENT + 1)}, root_s)
    assert "超长" in out, out
    out = m.call_tool("hub_memory_write", {"content": "z" * (m.MAX_CONTENT + 1)}, root_s)
    assert "超长" in out, out
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "x",
                                       "content": "date畸形", "date": "明天下午"}, root_s)
    assert "已记入大脑" in out, out
    today = datetime.date.today().isoformat()
    assert any(r["date"] == today and "date畸形" in r["content"]
               for r in brain.list_records(root_s, project="测试-项目", limit=2000)), "date 兜底失败"
    out = m.call_tool("hub_get_progress", {"limit": "abc"}, root_s)
    assert "各 agent 最近工作" in out
    out = m.call_tool("hub_get_progress", {"limit": 99999}, root_s)
    assert "各 agent 最近工作" in out
    out = m.call_tool("hub_log_work", {"project": "", "agent": "x", "content": "y"}, root_s)
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
    # 四家目标定义完整
    assert {t["agent"] for t in core.BOOTSTRAP_TARGETS} == {"ZCode", "Claude Code", "Codex", "DSH"}
    for t in core.BOOTSTRAP_TARGETS:
        assert t["path"].startswith("~/")


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


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_mcp_")
    root = Path(tmp) / "hub"
    build_hub(root)
    print(f"临时目录：{tmp}\n")
    case("MCP协议（握手/22工具/未知方法/异常自动登记/ping）", lambda: t_protocol(root))
    case("MCP工具集（读写记录/搜索/公用记忆/进度/注入拦截）", lambda: t_tools(root))
    case("记忆overwrite语义（真清空+备份+非法mode）", lambda: t_memory_overwrite(tmp))
    case("8线程并发log_work（不丢行/journal完整）", lambda: t_concurrent_log_work(tmp))
    case("心跳撞车预警/陈旧清理/坏json自愈", lambda: t_heartbeat_conflict(fresh_hub(tmp, "hb")))
    case("错误登记（上报/查询/流转/坏行）", lambda: t_errors_flow(fresh_hub(tmp, "err")))
    case("撤销（只切自己最新段/误伤检查/栈式撤销）", lambda: t_undo_log(fresh_hub(tmp, "undo")))
    case("新工具（get_rules/create_project+注入拒绝）", lambda: t_new_tools(tmp))
    case("引导注入（幂等/移除还原/自动创建/四家目标）", lambda: t_bootstrap(tmp))
    case("端到端子进程握手", lambda: t_end_to_end(root))
    case("root动态跟随（迁移后旧进程写新库/不误写旧库）", lambda: t_root_follow(tmp))
    case("一键接入（两种布局/备份/移除/坏json/幂等）", lambda: t_mcp_access(tmp))
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("MCP ALL PASS")


if __name__ == "__main__":
    main()
