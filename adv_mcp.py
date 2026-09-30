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


def fresh_hub(tmp, name) -> Path:
    """独立 hub：隔离共享状态（错误自增 id / 心跳残留），保证用例可单跑复现。"""
    root = Path(tmp) / name
    build_hub(root)
    return root


def t_protocol(root):
    r = resp_ok(m.handle_message(rpc("initialize", {"protocolVersion": "2025-06-18"}), str(root)))
    assert r["protocolVersion"] == "2025-06-18" and r["serverInfo"]["name"] == "agenthub"
    assert r["serverInfo"]["version"] == "1.3.1"
    assert m.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, str(root)) is None
    tools = resp_ok(m.handle_message(rpc("tools/list"), str(root)))["tools"]
    names = {t["name"] for t in tools}
    assert {"hub_list_projects", "hub_log_work", "hub_memory_read", "hub_memory_write",
            "hub_get_progress", "hub_list_skills", "hub_list_mcps", "hub_search",
            "hub_get_project", "hub_create_project", "hub_get_rules", "hub_heartbeat",
            "hub_report_error", "hub_list_errors", "hub_undo"} <= names, names
    assert len(names) == 15
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
    errs = core.list_errors(str(root))
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
    assert "测试-项目" in out
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "deepseek",
                                       "content": "对抗测试记录：修了X，验证通过"}, root_s)
    assert "已记录" in out
    rec = (root / "测试-项目" / core.RECORD_NAME).read_text(encoding="utf-8")
    assert "（deepseek）" in rec and "对抗测试记录" in rec
    out = m.call_tool("hub_memory_write", {"content": "环境事实：校园网GitHub不通"}, root_s)
    assert "公用记忆" in out
    assert "校园网GitHub不通" in m.call_tool("hub_memory_read", {}, root_s)
    out = m.call_tool("hub_get_progress", {"limit": 10}, root_s)
    assert "deepseek" in out or "hermes" in out
    out = m.call_tool("hub_list_skills", {}, root_s)
    assert "技能" in out
    out = m.call_tool("hub_list_mcps", {}, root_s)
    assert "MCP" in out
    out = m.call_tool("hub_log_work", {"project": "没有-此项目", "agent": "x", "content": "y"}, root_s)
    assert "不存在" in out
    out = m.call_tool("hub_log_work", {"project": "", "agent": "x", "content": " "}, root_s)
    assert "必填" in out
    # 路径注入：project 名带穿越不允许写入项目外
    out = m.call_tool("hub_log_work", {"project": "..\\逃逸", "agent": "x", "content": "y"}, root_s)
    assert "不存在" in out


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
    assert d["mcp"]["servers"]["agenthub"]["args"] == [srv, root]
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

def t_memory_overwrite(tmp):
    root = str(Path(tmp) / "hub")
    out = m.call_tool("hub_memory_write", {"content": "旧记忆第一条"}, root)
    assert "append" in out
    out = m.call_tool("hub_memory_write", {"content": "覆写后的全新记忆", "mode": "overwrite",
                                           "agent": "zcode"}, root)
    assert "overwrite" in out
    mem = m.call_tool("hub_memory_read", {}, root)
    assert "覆写后的全新记忆" in mem, mem
    assert "旧记忆第一条" not in mem, "overwrite 没有清空旧内容！"
    # overwrite 产生备份（write_text_backed 路径）
    assert list((Path(root) / core.DIR_META).glob("memory.md.bak-agenthub-*")), "overwrite 未备份"
    # 非法 mode
    out = m.call_tool("hub_memory_write", {"content": "x", "mode": "drop"}, root)
    assert "append 或 overwrite" in out
    # 空 content
    out = m.call_tool("hub_memory_write", {"content": "  "}, root)
    assert "必填" in out
    # journal 有埋点
    jl = core.read_journal(root)
    assert any(e.get("action") == "memory_overwrite" for e in jl)


def t_concurrent_log_work(tmp):
    root = Path(tmp) / "hub"
    root_s = str(root)
    p = root / "并发-记录"
    p.mkdir()
    (p / core.RECORD_NAME).write_text("# 工作记录\n", encoding="utf-8")
    errs = []

    def worker(i):
        out = m.call_tool("hub_log_work", {"project": "并发-记录", "agent": f"并发{i}",
                                           "content": f"第{i}号并发记录内容", "date": "2026-09-30"}, root_s)
        if "已记录" not in out:
            errs.append(out)

    import threading
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    rec = (p / core.RECORD_NAME).read_text(encoding="utf-8")
    for i in range(8):
        assert f"（并发{i}）" in rec, f"并发第{i}条丢失"
        assert f"第{i}号并发记录内容" in rec, f"并发第{i}条内容交错丢失"
    # journal 每行合法 JSON
    jl = core.read_journal(root_s, limit=1000)
    log_entries = [e for e in jl if e.get("action") == "log_work" and e.get("agent", "").startswith("并发")]
    assert len(log_entries) == 8, f"journal 应有 8 条并发记录，实际 {len(log_entries)}"


def t_heartbeat_conflict(root):
    root = str(root)
    out = m.call_tool("hub_heartbeat", {"agent": "alpha", "project": "测试-项目", "note": "改UI"}, root)
    assert "无其他活跃会话" in out
    # 第二个 agent 同项目 -> 收到撞车预警
    out = m.call_tool("hub_heartbeat", {"agent": "beta", "project": "测试-项目", "note": "改驱动"}, root)
    assert "撞车预警" in out and "alpha" in out, out
    # log_work 返回值也带预警（alpha/beta 心跳都还活跃且同项目）
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "alpha",
                                       "content": "alpha 的活干完了"}, root)
    assert "撞车预警" in out, out
    # 不同项目不预警
    out = m.call_tool("hub_heartbeat", {"agent": "beta", "project": "别的-项目"}, root)
    assert "无其他活跃会话" in out
    # 陈旧清理：把 alpha 的 ts 手改到 1 小时前 -> beta 再心跳时 alpha 被清理
    sf = Path(root) / core.DIR_META / "sessions.json"
    data = json.loads(sf.read_text(encoding="utf-8"))
    for s in data:
        if s.get("agent") == "alpha":
            s["ts"] = "2026-09-30T08:00:00"
    sf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    out = m.call_tool("hub_heartbeat", {"agent": "beta", "project": "测试-项目"}, root)
    assert "alpha" not in out, "陈旧会话未被清理"
    data = json.loads(sf.read_text(encoding="utf-8"))
    assert all(s.get("agent") != "alpha" for s in data)
    # 坏 json 自愈：备份后重建
    sf.write_text("{坏掉的json", encoding="utf-8")
    out = m.call_tool("hub_heartbeat", {"agent": "gamma", "project": "x-y"}, root)
    assert "心跳已更新" in out
    json.loads(sf.read_text(encoding="utf-8"))  # 重建为合法 json
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
    assert "#1" in out and "删错文件" in out and "从回收站还原" in out
    assert "[open]" in out
    # 状态流转
    assert core.set_error_status(root, 1, "fixed") == ""
    out = m.call_tool("hub_list_errors", {"status": "fixed"}, root)
    assert "删错文件" in out
    out = m.call_tool("hub_list_errors", {"status": "open"}, root)
    assert "删错文件" not in out
    # 非法 status / 不存在 id
    assert "open/fixed" in core.set_error_status(root, 1, "bad")
    assert "未找到" in core.set_error_status(root, 99, "fixed")
    # 自增 id
    m.call_tool("hub_report_error", {"agent": "x", "title": "第二件错事"}, root)
    errs = core.list_errors(root)
    assert errs[0]["id"] == 2
    # title 必填
    out = m.call_tool("hub_report_error", {"agent": "x", "title": " "}, root)
    assert "必填" in out


def t_undo_log(root):
    root_s = str(root)
    rec = Path(root_s) / "测试-项目" / core.RECORD_NAME
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "undoA",
                                       "content": "undoA 第一件事", "date": "2026-09-29"}, root_s)
    assert "已记录" in out, out
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "undoA",
                                       "content": "undoA 第二件事写错了", "date": "2026-09-30"}, root_s)
    assert "已记录" in out, out
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "undoB",
                                       "content": "undoB 的事不能被误伤", "date": "2026-09-30"}, root_s)
    assert "已记录" in out, out
    # 撤 undoA 最新一条
    out = m.call_tool("hub_undo", {"agent": "undoA"}, root_s)
    assert "已撤销" in out, out
    text = rec.read_text(encoding="utf-8")
    assert "第二件事写错了" not in text, "目标段未切除"
    assert "undoA 第一件事" in text, "误伤更早的段"
    assert "undoB 的事不能被误伤" in text, "误伤他人段"
    assert "初始化记录" in text, "误伤文件原有内容"
    # 重复撤销 = 栈式继续撤上一条（09-29 段），他人段与原文不动
    out = m.call_tool("hub_undo", {"agent": "undoA"}, root_s)
    assert "已撤销" in out, out
    text = rec.read_text(encoding="utf-8")
    assert "undoA 第一件事" not in text
    assert "undoB 的事不能被误伤" in text and "初始化记录" in text
    # 撤无可撤
    out = m.call_tool("hub_undo", {"agent": "undoA"}, root_s)
    assert "没有找到" in out, out
    # 无记录的 agent
    out = m.call_tool("hub_undo", {"agent": "没干活的"}, root_s)
    assert "没有找到" in out, out
    # agent 必填
    out = m.call_tool("hub_undo", {"agent": ""}, root_s)
    assert "必填" in out
    # 撤销操作进流水，且原文有备份
    jl = core.read_journal(root_s, limit=1000)
    assert len([e for e in jl if e.get("action") == "undo_log_work"]) == 2
    assert list(rec.parent.glob("工作记录.md.bak-agenthub-*"))


def t_new_tools(tmp):
    root_s = str(Path(tmp) / "hub")
    # 参数健壮化（v1.3.1）：content 超长拒绝、date 畸形兜底、limit 容错
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "x",
                                       "content": "y" * (m.MAX_CONTENT + 1)}, root_s)
    assert "超长" in out, out
    out = m.call_tool("hub_memory_write", {"content": "z" * (m.MAX_CONTENT + 1)}, root_s)
    assert "超长" in out, out
    out = m.call_tool("hub_log_work", {"project": "测试-项目", "agent": "x",
                                       "content": "date畸形", "date": "明天下午"}, root_s)
    assert "已记录" in out, out
    today = datetime.date.today().isoformat()
    assert f"## {today}（x）" in (Path(root_s) / "测试-项目" / core.RECORD_NAME).read_text(encoding="utf-8")
    out = m.call_tool("hub_get_progress", {"limit": "abc"}, root_s)
    assert "各 agent 最近工作" in out
    out = m.call_tool("hub_get_progress", {"limit": 99999}, root_s)
    assert "各 agent 最近工作" in out
    out = m.call_tool("hub_log_work", {"project": "", "agent": "x", "content": "y"}, root_s)
    assert "project 必填" in out
    # get_rules：未初始化时回退内置规则
    out = m.call_tool("hub_get_rules", {}, root_s)
    assert "对象-问题" in out
    # 初始化后读文件内容
    (Path(root_s) / core.DIR_META).mkdir(exist_ok=True)
    (Path(root_s) / core.DIR_META / core.RULES_NAME).write_text("自定义规则v1", encoding="utf-8")
    assert m.call_tool("hub_get_rules", {}, root_s) == "自定义规则v1"
    # create_project 成功 + journal 埋点
    out = m.call_tool("hub_create_project", {"project": "新工具新建-项目", "agent": "zcode"}, root_s)
    assert "已创建" in out
    assert (Path(root_s) / "新工具新建-项目" / "input").is_dir()
    assert any(e.get("action") == "create_project" for e in core.read_journal(root_s))
    # 注入拒绝：穿越 / 保留名 / 无连字符
    out = m.call_tool("hub_create_project", {"project": "../逃逸"}, root_s)
    assert "错误" in out
    assert not (Path(root_s).parent / "逃逸").exists()
    out = m.call_tool("hub_create_project", {"project": "CON"}, root_s)
    assert "保留名" in out
    out = m.call_tool("hub_create_project", {"project": "没有连字符"}, root_s)
    assert "连字符" in out
    # 重复创建幂等
    out = m.call_tool("hub_create_project", {"project": "新工具新建-项目"}, root_s)
    assert "已创建" in out


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


def main():
    tmp = tempfile.mkdtemp(prefix="agenthub_mcp_")
    root = Path(tmp) / "hub"
    build_hub(root)
    print(f"临时目录：{tmp}\n")
    case("MCP协议（握手/15工具/未知方法/异常自动登记/ping）", lambda: t_protocol(root))
    case("MCP工具集（读写记录/搜索/公用记忆/进度/注入拦截）", lambda: t_tools(root))
    case("记忆overwrite语义（真清空+备份+非法mode）", lambda: t_memory_overwrite(tmp))
    case("8线程并发log_work（不丢行/journal完整）", lambda: t_concurrent_log_work(tmp))
    case("心跳撞车预警/陈旧清理/坏json自愈", lambda: t_heartbeat_conflict(fresh_hub(tmp, "hb")))
    case("错误登记（上报/查询/流转/坏行）", lambda: t_errors_flow(fresh_hub(tmp, "err")))
    case("撤销（只切自己最新段/误伤检查/栈式撤销）", lambda: t_undo_log(fresh_hub(tmp, "undo")))
    case("新工具（get_rules/create_project+注入拒绝）", lambda: t_new_tools(tmp))
    case("引导注入（幂等/移除还原/自动创建/四家目标）", lambda: t_bootstrap(tmp))
    case("端到端子进程握手", lambda: t_end_to_end(root))
    case("一键接入（两种布局/备份/移除/坏json/幂等）", lambda: t_mcp_access(tmp))
    print()
    if FAILED:
        print(f"未通过 {len(FAILED)} 项：{'、'.join(FAILED)}")
        sys.exit(1)
    print("MCP ALL PASS")


if __name__ == "__main__":
    main()
