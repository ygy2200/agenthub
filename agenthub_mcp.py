# -*- coding: utf-8 -*-
"""AgentHub MCP server —— 公用大脑接入层。

零第三方依赖，手写 MCP stdio 传输（newline-delimited JSON-RPC 2.0）。
被各 agent 以子进程方式启动：
    D:\\python311\\python.exe agenthub_mcp.py <AgentHub根目录>

协议帧：每行一条 JSON 消息。日志只写 stderr，stdout 仅承载协议。
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agentscore  # noqa: E402
import core  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "agenthub", "version": "1.3.0"}


# ---------------------------------------------------------------- 工具实现（纯函数，供测试直接调用）

def memory_path(root: str) -> Path:
    return core.memory_file(root)


def _conflict_warn(active: list) -> str:
    if not active:
        return ""
    others = "、".join(f"{s.get('agent')}（{s.get('note') or '工作中'}，{s.get('ts', '')}）" for s in active)
    return f"\n⚠ 撞车预警：同项目还有其他活跃会话：{others}，注意分工避让"


def call_tool(name: str, arguments: dict, root: str) -> str:
    """执行一个工具，返回文本结果。抛异常时由协议层转为 isError 并自动登记错误。"""
    if not root or not os.path.isdir(root):
        return f"错误：AgentHub 根目录不存在：{root}"

    if name == "hub_list_projects":
        snap = core.scan(root)
        lines = [f"共 {len(snap.projects)} 个项目（按最近活动排序，前 50）："]
        for p in snap.projects[:50]:
            n_rec = len(p.records)
            lines.append(f"- {p.name}  最近活动:{p.last_active or '无'}  记录:{n_rec}条  路径:{p.path}")
        return "\n".join(lines)

    if name == "hub_get_project":
        pname = str(arguments.get("project", "")).strip()
        snap = core.scan(root)
        proj = next((p for p in snap.projects if p.name == pname), None)
        if not proj:
            close = [p.name for p in snap.projects if pname.lower() in p.name.lower()][:8]
            return f"项目不存在：{pname}。相近项目：{'、'.join(close) if close else '无'}"
        out = [f"项目：{proj.name}", f"路径：{proj.path}", f"最近活动：{proj.last_active or '无'}",
               f"文件：{'、'.join(rp for _, rp, _ in proj.files[:20]) or '无'}", "", "最近记录："]
        for r in proj.records[-5:]:
            out.append(f"[{r.date or '无日期'}|{r.agent}] {r.title}")
            if r.body:
                out.append("  " + r.body[:300].replace("\n", "\n  "))
        return "\n".join(out)

    if name == "hub_log_work":
        pname = str(arguments.get("project", "")).strip()
        agent = str(arguments.get("agent", "unknown")).strip() or "unknown"
        content = str(arguments.get("content", "")).strip()
        date = str(arguments.get("date") or datetime.date.today().isoformat())
        if not pname or not content:
            return "错误：project 与 content 必填"
        proj_dir = Path(root) / pname
        if not proj_dir.is_dir():
            return (f"错误：项目目录不存在：{pname}（新项目先调 hub_create_project，"
                    f"或用 hub_list_projects 核对现有项目名）")
        rec = proj_dir / core.RECORD_NAME
        entry = f"{date}（{agent}）"
        with core.hub_lock(root):
            with open(rec, "a", encoding="utf-8") as f:
                f.write(f"\n## {entry}\n{content}\n")
        core.journal(root, agent, "log_work", str(rec), note=entry)
        err, active = core.heartbeat(root, agent, pname)
        warn = _conflict_warn(active)
        return f"已记录到 {rec}（追加 {len(content)} 字符）{warn}"

    if name == "hub_create_project":
        pname = str(arguments.get("project", "")).strip()
        agent = str(arguments.get("agent", "unknown")).strip()
        err = core.create_project(root, pname)
        if err:
            return f"错误：{err}"
        core.journal(root, agent or "unknown", "create_project",
                     str(Path(root) / pname))
        return f"项目已创建：{pname}（含 input/output 与工作记录模板）"

    if name == "hub_search":
        kw = str(arguments.get("keyword", "")).strip()
        hits = core.search(root, kw, max_hits=20)
        if not hits:
            return f"无结果：{kw}"
        return "\n".join(f"- [{h.project}] {Path(h.path).name}:{h.line_no}  {h.line[:120]}" for h in hits)

    if name == "hub_get_rules":
        return core.load_rules(root)

    if name == "hub_memory_read":
        f = memory_path(root)
        if not f.is_file():
            return "（公用记忆为空。这是所有 agent 共享的记忆区，可写入项目进展、环境事实、用户偏好等）"
        return core.read_text(f)

    if name == "hub_memory_write":
        content = str(arguments.get("content", "")).strip()
        mode = str(arguments.get("mode", "append"))
        agent = str(arguments.get("agent", "unknown")).strip() or "unknown"
        if not content:
            return "错误：content 必填"
        if mode not in ("append", "overwrite"):
            return "错误：mode 只能是 append 或 overwrite"
        f = memory_path(root)
        with core.hub_lock(root):
            if mode == "overwrite":
                err = core.write_text_backed(str(f), content + "\n", root=root, agent=agent,
                                             action="memory_overwrite")
                if err:
                    return f"错误：{err}"
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                with open(f, "a", encoding="utf-8") as fh:
                    fh.write(f"\n[{stamp}] {content}\n")
        core.journal(root, agent, f"memory_{mode}", str(f))
        return f"已写入公用记忆（{mode}），当前所有 agent 可读"

    if name == "hub_heartbeat":
        agent = str(arguments.get("agent", "")).strip()
        project = str(arguments.get("project", "")).strip()
        note = str(arguments.get("note", "")).strip()
        err, active = core.heartbeat(root, agent, project, note)
        if err:
            return f"错误：{err}"
        return ("心跳已更新" + _conflict_warn(active)) if active else "心跳已更新（同项目无其他活跃会话）"

    if name == "hub_report_error":
        agent = str(arguments.get("agent", "unknown")).strip() or "unknown"
        title = str(arguments.get("title", "")).strip()
        detail = str(arguments.get("detail", "")).strip()
        project = str(arguments.get("project", "")).strip()
        undo = str(arguments.get("undo", "")).strip()
        err = core.report_error(root, agent, title, detail, project, undo)
        return f"错误已登记，其他 agent 与用户可在 AgentHub 流水页看到" if not err else f"错误：{err}"

    if name == "hub_list_errors":
        status = str(arguments.get("status", "")).strip()
        errs = core.list_errors(root, status if status in ("open", "fixed") else "")
        if not errs:
            return "（无错误登记）"
        lines = [f"共 {len(errs)} 条错误登记（新在前）："]
        for e in errs[:30]:
            lines.append(f"- #{e.get('id')} [{e.get('status')}] {e.get('ts', '')} {e.get('agent')}·"
                         f"{e.get('project') or '无项目'}：{e.get('title')}")
            if e.get("undo"):
                lines.append(f"  回滚方式：{e['undo']}")
        return "\n".join(lines)

    if name == "hub_undo":
        agent = str(arguments.get("agent", "")).strip()
        if not agent:
            return "错误：agent 必填（只允许撤销自己的记录）"
        err, bak = core.undo_log(root, agent)
        if err:
            return f"错误：{err}"
        return f"已撤销该 agent 最近一条 hub 记录（原文备份：{bak}）"

    if name == "hub_list_skills":
        want = str(arguments.get("agent", "")).strip().lower()
        infos = agentscore.detect_agents(core.load_config().get("extra_agents"))
        lines = []
        for a in infos:
            if want and want not in a.name.lower():
                continue
            lines.append(f"[{a.name}] {len(a.skills)} 个技能：")
            for s in a.skills[:60]:
                lines.append(f"  - {s.name}：{s.desc[:80]}")
            if len(a.skills) > 60:
                lines.append(f"  ...（共 {len(a.skills)} 个）")
        return "\n".join(lines) if lines else f"未找到 agent：{want}"

    if name == "hub_list_mcps":
        infos = agentscore.detect_agents(core.load_config().get("extra_agents"))
        lines = ["本机已配置的 MCP 服务器："]
        merged: dict = {}
        for a in infos:
            for m in a.mcps:
                merged.setdefault(m.name, []).append(a.name)
        for server_name, agents_ in sorted(merged.items()):
            lines.append(f"- {server_name}  配置于 {'、'.join(agents_)}")
        return "\n".join(lines)

    if name == "hub_get_progress":
        limit = int(arguments.get("limit", 30))
        snap = core.scan(root)
        entries = sorted((r for p in snap.projects for r in p.records),
                         key=lambda r: (r.date, r.project), reverse=True)[:limit]
        by_agent: dict = {}
        for r in entries:
            by_agent.setdefault(r.agent or "其他", []).append(r)
        lines = ["各 agent 最近工作（对齐进度用）："]
        for a, rs in sorted(by_agent.items()):
            lines.append(f"[{a}]")
            for r in rs[:10]:
                lines.append(f"  {r.date} {r.project}：{r.title[:60]}")
        return "\n".join(lines)

    return f"未知工具：{name}"


TOOLS = [
    {"name": "hub_list_projects", "description": "列出 AgentHub 全部项目（名称/最近活动/记录数）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_get_project", "description": "查看某项目的路径、文件与最近工作记录",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string", "description": "项目目录名"}}, "required": ["project"]}},
    {"name": "hub_create_project", "description": "按规范新建项目目录（「对象-问题」命名，含 input/output 与工作记录模板）",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "项目目录名，须含连字符"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["project"]}},
    {"name": "hub_log_work", "description": "向项目工作记录.md 追加一条工作记录（所有 agent 应在完成任务后调用；走此工具的记录可撤销、进操作流水）",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string"}, "agent": {"type": "string", "description": "你的 agent 名，如 ZCode/hermes"},
                                    "content": {"type": "string", "description": "做了什么/验证结果/如何回滚"},
                                    "date": {"type": "string", "description": "YYYY-MM-DD，缺省今天"}},
                     "required": ["project", "agent", "content"]}},
    {"name": "hub_search", "description": "跨项目全文搜索工作记录与文件名",
     "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]}},
    {"name": "hub_get_rules", "description": "读取团队协作规范（目录命名/记录格式/铁律）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_memory_read", "description": "读取公用大脑记忆（所有 agent 共享的知识：环境事实/用户偏好/项目进展）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_memory_write", "description": "写入公用大脑记忆，供其他 agent 读取（append 追加带时间戳，overwrite 会先自动备份再覆写）",
     "inputSchema": {"type": "object",
                     "properties": {"content": {"type": "string"},
                                    "mode": {"type": "string", "enum": ["append", "overwrite"]},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["content"]}},
    {"name": "hub_heartbeat", "description": "会话心跳：登记自己正在哪个项目干活。同项目有其他 agent 活跃时会收到撞车预警，长任务开工前先调用",
     "inputSchema": {"type": "object",
                     "properties": {"agent": {"type": "string", "description": "你的 agent 名"},
                                    "project": {"type": "string", "description": "正在工作的项目名"},
                                    "note": {"type": "string", "description": "一句话说明在做什么"}},
                     "required": ["agent"]}},
    {"name": "hub_report_error", "description": "登记错误/踩坑（含回滚方式），用户与其他 agent 可在流水页查询。出错时主动调用",
     "inputSchema": {"type": "object",
                     "properties": {"title": {"type": "string", "description": "一句话错误摘要"},
                                    "detail": {"type": "string", "description": "现象/原因/过程"},
                                    "project": {"type": "string", "description": "相关项目名，可空"},
                                    "undo": {"type": "string", "description": "如何回滚/撤销"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["title"]}},
    {"name": "hub_list_errors", "description": "查询错误登记（可按 status=open/fixed 过滤），排查历史问题用",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["open", "fixed"]}}}},
    {"name": "hub_undo", "description": "撤销你（agent）最近一条经 hub_log_work 写入的记录段（原文自动备份，操作进流水）",
     "inputSchema": {"type": "object",
                     "properties": {"agent": {"type": "string", "description": "你的 agent 名（只能撤自己的）"}},
                     "required": ["agent"]}},
    {"name": "hub_list_skills", "description": "列出本机各 agent 的技能库（能力对齐：看别的 agent 会什么）",
     "inputSchema": {"type": "object", "properties": {"agent": {"type": "string", "description": "可选，过滤 agent 名"}}}},
    {"name": "hub_list_mcps", "description": "列出本机各 agent 已配置的 MCP 服务器",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_get_progress", "description": "获取所有 agent 最近的工作时间线（进度对齐）",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "条数，默认30"}}}},
]


# ---------------------------------------------------------------- MCP 协议层

def handle_message(msg: dict, root: str) -> dict | None:
    """处理一条 JSON-RPC 请求，返回响应；通知返回 None。"""
    method = msg.get("method", "")
    mid = msg.get("id")
    is_notification = mid is None

    if method == "initialize":
        req_ver = (msg.get("params") or {}).get("protocolVersion", PROTOCOL_VERSION)
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"protocolVersion": req_ver, "capabilities": {"tools": {}},
                           "serverInfo": SERVER_INFO}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        tname = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            text = call_tool(tname, args, root)
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except Exception as e:  # noqa: BLE001  工具错误以 isError 返回，不断连，并自动落盘错误登记
            text = f"工具执行失败：{type(e).__name__}: {e}"
            try:
                core.report_error(root, str(args.get("agent") or "mcp-server"),
                                  f"MCP 工具 {tname} 执行异常", text,
                                  project=str(args.get("project") or ""))
            except Exception:  # noqa: BLE001 登记失败不影响协议响应
                pass
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": text}],
                               "isError": True}}
    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(root: str) -> int:
    """stdio 主循环。EOF/键盘中断退出。"""
    log = lambda s: print(f"[agenthub-mcp] {s}", file=sys.stderr)  # noqa: E731
    log(f"启动，根目录：{root}")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            log(f"坏消息（{e}）：{raw[:100]}")
            continue
        try:
            resp = handle_message(msg, root)
        except Exception as e:  # noqa: BLE001
            log(f"处理异常：{type(e).__name__}: {e}")
            resp = ({"jsonrpc": "2.0", "id": msg.get("id"),
                     "error": {"code": -32603, "message": "internal error"}} if msg.get("id") is not None else None)
        if resp:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else core.get_root()
    sys.exit(serve(root))


if __name__ == "__main__":
    main()
