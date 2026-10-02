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

# stdio 一律按 UTF-8 处理（2026-09-30 dsh 修，复盘时从部署版回流）：
# Windows 上 Python 子进程的 stdin/stdout 默认跟随系统 ANSI 代码页（实测 stdin.encoding == 'gbk'），
# 而 MCP 客户端按 UTF-8 发帧，中文参数会被解码成孤立代理字符，报
# "UnicodeEncodeError: surrogates not allowed"。三流都钉死 UTF-8，
# 客户端就不再需要设 PYTHONIOENCODING。重配失败（如被测试框架替换成非标准流）不致命，忽略即可。
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

import agentscore  # noqa: E402
import brain  # noqa: E402
import core  # noqa: E402

# 流水落库（brain.db）；文件版仅作兜底
core.JOURNAL_SINK = brain.journal_add

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "agenthub", "version": "2.0.0"}
MAX_CONTENT = 128 * 1024  # 单条记录/记忆写入上限，防 agent 失控灌爆


# ---------------------------------------------------------------- 工具实现（纯函数，供测试直接调用）

def memory_path(root: str) -> Path:
    return core.memory_file(root)


def _conflict_warn(active: list) -> str:
    if not active:
        return ""
    others = "、".join(f"{s.get('agent')}（{s.get('note') or '工作中'}，{s.get('ts', '')}）" for s in active)
    return f"\n⚠ 撞车预警：同项目还有其他活跃会话：{others}，注意分工避让"


def _check_content(content: str, what: str) -> str:
    """内容校验，通过返回 ""，否则返回错误文本。"""
    if not content:
        return f"错误：{what}必填"
    if len(content) > MAX_CONTENT:
        return f"错误：{what}超长（{len(content)} > {MAX_CONTENT} 字符），请精简后分条写入"
    return ""


def _guess_agent(root: str) -> str:
    """从活跃会话推断检索者身份：单会话时可靠；多会话取最近心跳者并加 ? 标注不确定。"""
    try:
        rows = brain.active_sessions(root)
        if len(rows) == 1:
            return rows[0]["agent"]
        if rows:
            return rows[0]["agent"] + "?"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _auto_title(content: str, date: str, agent: str) -> str:
    """记录标题自动摘要：取「目的」行或首个非空行，避免标题全部是重复的日期串
    （2026-10-01 用户实测：dsh 记录标题清一色"2026-10-01（dsh）"，时间线毫无信息量）。"""
    import re
    m = re.search(r"目的[】\]:：]\s*(.+)", content)
    first = (m.group(1) if m else next((ln.strip() for ln in content.splitlines() if ln.strip()), ""))
    first = first.strip("【】 ").strip()
    if first:
        return f"{date}（{agent}）· {first[:48]}{'…' if len(first) > 48 else ''}"
    return f"{date}（{agent}）"


def _snippet(content: str, words: list, width: int = 76) -> str:
    """命中片段：取首个命中词前后的上下文（无命中给开头），单行化。
    hub_search 只给标题时判断不了价值，精读前先看片段省一次 hub_get_record。"""
    text = " ".join((content or "").split())
    if not text:
        return ""
    pos = -1
    for w in words:
        pos = text.lower().find(w.lower())
        if pos >= 0:
            break
    if pos < 0:
        pos = 0
    start = max(0, pos - 20)
    frag = text[start:start + width]
    head = "…" if start > 0 else ""
    tail = "…" if start + width < len(text) else ""
    return f"{head}{frag}{tail}"


def call_tool(name: str, arguments: dict, root: str) -> str:
    """执行一个工具，返回文本结果。抛异常时由协议层转为 isError 并自动登记错误。"""
    if not root or not os.path.isdir(root):
        return f"错误：AgentHub 根目录不存在：{root}"

    if name == "hub_list_projects":
        rows = brain.list_projects(root)
        lines = [f"共 {len(rows)} 个项目（按最近活动排序，前 50）："]
        for p in rows:
            mark = "  [停滞]" if p.get("status") == "stalled" else ""
            lines.append(f"- {p['name']}  最近活动:{p['last_active'] or '无'}  记录:{p['n_records']}条{mark}")
        return "\n".join(lines)

    if name == "hub_get_project":
        pname = str(arguments.get("project", "")).strip()
        proj = brain.get_project(root, pname)
        if not proj:
            close = [p["name"] for p in brain.list_projects(root, 200) if pname.lower() in p["name"].lower()][:8]
            return f"项目不存在：{pname}。相近项目：{'、'.join(close) if close else '无'}"
        out = [f"项目：{proj['name']}", f"归属:{proj['agent'] or '未知'}  最近活动：{proj['last_active'] or '无'}",
               "", "最近记录："]
        for r in proj["records"]:
            out.append(f"[{r['date'] or '无日期'}|{r['agent']}] {r['title']}")
            if r["content"]:
                out.append("  " + r["content"][:300].replace("\n", "\n  "))
        return "\n".join(out)

    if name == "hub_log_work":
        pname = str(arguments.get("project", "")).strip()
        agent = str(arguments.get("agent", "unknown")).strip() or "unknown"
        content = str(arguments.get("content", "")).strip()
        err = _check_content(content, "content")
        if err:
            return err
        if not pname:
            return "错误：project 必填"
        # date 畸形兜底：从参数里提取 YYYY-MM-DD，取不到用今天
        dm = core.DATE_RE.search(str(arguments.get("date") or ""))
        date = dm.group(1) if dm else datetime.date.today().isoformat()
        rec_id = brain.add_record(root, pname, agent, date, _auto_title(content, date, agent), content)
        core.journal(root, agent, "log_work", f"brain:records#{rec_id}", note=f"{date}（{agent}）")
        herr, active = brain.heartbeat_touch(root, agent, pname)
        warn = _conflict_warn(active)
        return f"已记入大脑（records#{rec_id}，项目 {pname}）{warn}"

    if name == "hub_create_project":
        pname = str(arguments.get("project", "")).strip()
        agent = str(arguments.get("agent", "unknown")).strip()
        err = core.create_project(root, pname)
        if err:
            return f"错误：{err}"
        with brain.db_conn(root) as conn:
            conn.execute("INSERT OR IGNORE INTO projects(name, created) VALUES(?,?)", (pname, brain._now()))
        core.journal(root, agent or "unknown", "create_project", pname)
        return f"项目已创建：{pname}（含 input/output 目录，记录存大脑数据库）"

    if name == "hub_search":
        kw = str(arguments.get("keyword", "")).strip()
        res = brain.search_all(root, kw)
        n_total = len(res["records"]) + len(res["memories"]) + len(res["files"])
        brain.log_search(root, "hub_search", kw, n_total, agent=_guess_agent(root))
        if not n_total:
            return (f"无结果：{kw}\n"
                    "（可试：① 拆成更短的词重查（按子串匹配）② hub_list_projects 看项目名 "
                    "③ hub_memory_read 查记忆 ④ hub_env_list 查环境档案）")
        words = [w for w in kw.split() if w][:8]
        lines = [f"全脑检索「{kw}」：命中 记录{len(res['records'])} · 记忆{len(res['memories'])} · "
                 f"文件{len(res['files'])}（记录行带 #id，精读用 hub_get_record）"]
        if any("_bigram" in r for r in res["records"]) or any("_bigram" in mem for mem in res["memories"]):
            lines.insert(1, "（连续长串无直接命中，已拆词放宽召回——按相关度排序，弱相关自行取舍）")
        for r in res["records"]:
            lines.append(f"[记录#{r['id']}] {r['date']} {r['project']}（{r['agent']}）：{r['title'][:60]}")
            snip = _snippet(r["content"], words)
            if snip:
                lines.append(f"  ↳ {snip}")
        for mem in res["memories"]:
            lines.append(f"[记忆#{mem['id']}] {brain.KIND_CN.get(mem['kind'], mem['kind'])}：{mem['content'][:100]}")
        for f in res["files"]:
            lines.append(f"[文件] {f['project']}\\{f['name']}")
        if len(lines) > 41:  # 头部 1 行 + 内容 40 行
            lines = lines[:41] + ["（结果较多仅显示前 40 行，换更具体的关键词可缩小范围）"]
        return "\n".join(lines)

    if name == "hub_get_rules":
        return core.load_rules(root)

    if name == "hub_memory_read":
        query = str(arguments.get("query", "")).strip()
        kind = str(arguments.get("kind", "")).strip()
        try:
            limit = int(arguments.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        rows = brain.search_memories(root, query, kind, limit)
        brain.log_search(root, "hub_memory_read", query or (f"kind:{kind}" if kind else ""), len(rows),
                         agent=_guess_agent(root))
        total = brain.count_memories(root)
        if not rows:
            msg = f"（大脑记忆无命中。当前共 {total} 条记忆；写入用 hub_memory_write）"
            if query:
                # 兜底：记忆 37 条只是结晶层，1486 条记录才是知识主体——0 命中不死路，
                # 自动用同 query 搜记录给线索（search_records 内部含 bigram 重试）
                recs = brain.search_records(root, query, 3)
                if recs:
                    note = "长串已拆词放宽召回，弱相关自行取舍；" if any("_bigram" in r for r in recs) else ""
                    lines = [msg, f"记忆之外，工作记录里可能有相关线索（{note}hub_get_record #id 精读）："]
                    lines += [f"[记录#{r['id']}] {r['date']} {r['project']}（{r['agent']}）：{(r['title'] or '')[:60]}"
                              for r in recs]
                    return "\n".join(lines)
            return msg
        head = f"大脑记忆（共 {total} 条" + (f"，命中 {len(rows)} 条" if query or kind else "") + "）"
        if any("_bigram" in m for m in rows):
            head += "（长串已拆词放宽召回，弱相关自行取舍）"
        lines = [head]
        for m in rows:
            flag = "★" if m["pinned"] else "·"
            tags = f" #{m['tags']}" if m["tags"] else ""
            lines.append(f"{flag} #{m['id']} [{brain.KIND_CN.get(m['kind'], m['kind'])}]{tags} {m['content'][:160]}")
        lines.append("（hub_memory_write 写入；read 带 query/kind/limit 参数可检索）")
        return "\n".join(lines)

    if name == "hub_memory_write":
        content = str(arguments.get("content", "")).strip()
        mode = str(arguments.get("mode", "append"))
        kind = str(arguments.get("kind", "note")).strip()
        tags = str(arguments.get("tags", "")).strip()
        project = str(arguments.get("project", "")).strip()
        agent = str(arguments.get("agent", "unknown")).strip() or "unknown"
        pinned = bool(arguments.get("pinned", False))
        err = _check_content(content, "content")
        if err:
            return err
        if mode not in ("append", "overwrite", "new"):
            return "错误：mode 只能是 append/new 或 overwrite"
        if mode == "overwrite":
            # overwrite 语义：按内容精确匹配更新旧条目（v1 文本时代遗留接口），否则当新条目
            rows = brain.search_memories(root, content[:80], "", 1)
            if rows and rows[0]["content"] == content:
                brain.edit_memory(root, rows[0]["id"], content=content, kind=kind if kind in brain.KINDS else "")
                core.journal(root, agent, "memory_overwrite", f"brain:memories#{rows[0]['id']}")
                return f"已更新大脑记忆 #{rows[0]['id']}"
        mid = brain.add_memory(root, content, kind, tags, project, agent, pinned)
        core.journal(root, agent, "memory_append", f"brain:memories#{mid}", note=kind)
        return f"已写入大脑记忆 #{mid}（{brain.KIND_CN.get(kind, kind)}），所有 agent 可读可检索"

    if name == "hub_heartbeat":
        agent = str(arguments.get("agent", "")).strip()
        project = str(arguments.get("project", "")).strip()
        note = str(arguments.get("note", "")).strip()
        # 免推判定须在心跳写入前读旧会话（heartbeat_touch 会重建 sessions）
        push = brain.should_push(root, agent, project)
        err, active = brain.heartbeat_touch(root, agent, project, note)
        if err:
            return f"错误：{err}"
        out = ("心跳已更新" + _conflict_warn(active)) if active else "心跳已更新（同项目无其他活跃会话）"
        # 大脑推送（反射弧）：开工即唤起该项目相关+置顶记忆，不靠 agent 自觉查询；
        # 免推窗口内的重复心跳不重推（内容还在会话上下文里），推送异常降级登记错误不打断心跳
        if push:
            try:
                recall = brain.recall_for(root, project)
            except Exception as e:  # noqa: BLE001
                recall = []
                try:
                    brain.error_add(root, "system", "记忆推送失败（heartbeat）",
                                    f"{type(e).__name__}: {e}", project)
                except Exception:  # noqa: BLE001
                    pass
            if recall:
                lines = [out, "", f"[大脑推送] 开工先读（{project or '全局'}相关/置顶记忆，共 {len(recall)} 条）："]
                for m in recall:
                    flag = "★" if m["pinned"] else "·"
                    lines.append(f"{flag} #{m['id']} [{brain.KIND_CN.get(m['kind'], m['kind'])}] {m['content'][:120]}")
                out = "\n".join(lines)
        return out

    if name == "hub_report_error":
        agent = str(arguments.get("agent", "unknown")).strip() or "unknown"
        title = str(arguments.get("title", "")).strip()
        detail = str(arguments.get("detail", "")).strip()
        project = str(arguments.get("project", "")).strip()
        undo = str(arguments.get("undo", "")).strip()
        err = brain.error_add(root, agent, title, detail, project, undo)
        return "错误已登记进大脑，用户与其他 agent 可在流水页/查询工具看到" if not err else f"错误：{err}"

    if name == "hub_list_errors":
        status = str(arguments.get("status", "")).strip()
        errs = brain.error_list(root, status if status in ("open", "fixed") else "")
        if not errs:
            return "（无错误登记）"
        lines = [f"共 {len(errs)} 条错误登记（新在前）："]
        for e in errs[:30]:
            lines.append(f"- #{e['id']} [{e['status']}] {e['ts']} {e['agent']}·"
                         f"{e['project'] or '无项目'}：{e['title']}")
            if e["undo"]:
                lines.append(f"  回滚方式：{e['undo']}")
        return "\n".join(lines)

    if name == "hub_undo":
        agent = str(arguments.get("agent", "")).strip()
        if not agent:
            return "错误：agent 必填（只允许撤销自己的记录）"
        err, info = brain.undo_last_record(root, agent)
        if err:
            return f"错误：{err}"
        core.journal(root, agent, "undo_log_work", "brain:records", note=info)
        return f"已撤销该 agent 最近一条 hub 记录（软删，可在流水追溯）：{info}"

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

    if name == "hub_list_agents":
        rows = brain.list_agents(root)
        if not rows:
            return "注册表为空（agent 首次心跳/写记录时自动登记）"
        lines = ["注册 agent（在线 = 心跳存活）："]
        for r in rows:
            mark = "[在线]" if r["online"] else "[离线]"
            lines.append(f"- {r['name']}  {mark}  记录{r['records']} · 心跳{r['heartbeats']} · "
                         f"最近项目:{r['last_project'] or '无'} · 最近活跃:{r['last_seen'] or '无'}")
        return "\n".join(lines)

    if name == "hub_list_todos":
        try:
            limit = int(arguments.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        todos = brain.extract_todos(root, limit)
        if not todos:
            return "未发现待办/承诺线索（记录里没有 待办|后续|下次|待验证…句式）"
        lines = [f"待办/承诺线索 {len(todos)} 条（新在前）："]
        for t in todos:
            lines.append(f"- [{t['date']} {t['agent']}·{t['project']}] {t['todo'][:80]}")
        return "\n".join(lines)

    if name == "hub_todo_done":
        try:
            rec_id = int(arguments.get("record_id", 0) or 0)
        except (TypeError, ValueError):
            rec_id = 0
        todo = str(arguments.get("todo", "")).strip()
        agent = str(arguments.get("agent", "")).strip()
        err = brain.mark_todo_done(root, rec_id, todo, agent)
        if err:
            return f"错误：{err}"
        core.journal(root, agent or "unknown", "todo_done", f"records#{rec_id}", note=todo[:60])
        return f"待办已勾销：{todo[:60]}"

    if name == "hub_env_set":
        category = str(arguments.get("category", "")).strip()
        key = str(arguments.get("key", "")).strip()
        value = str(arguments.get("value", "")).strip()
        agent = str(arguments.get("agent", "")).strip()
        err = brain.env_set(root, category, key, value, agent)
        if err:
            return f"错误：{err}"
        core.journal(root, agent or "unknown", "env_set", f"{category}/{key}")
        return f"环境档案已登记：[{category}] {key}"

    if name == "hub_env_list":
        category = str(arguments.get("category", "")).strip()
        kw = str(arguments.get("kw", "")).strip()
        try:
            limit = int(arguments.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100
        rows = brain.env_list(root, category, kw, limit)
        if not rows:
            return "环境档案为空或无匹配（agent 可先调 hub_env_set 登记，或让 GUI 跑一次自动采集）"
        lines = [f"环境档案 {len(rows)} 项："]
        for r in rows:
            lines.append(f"- [{r['category']}] {r['key']} = {r['value'][:70]}  ({r['updated'][:10]})")
        return "\n".join(lines)

    if name == "hub_search_files":
        query = str(arguments.get("query", "")).strip()
        try:
            limit = int(arguments.get("limit", 30))
        except (TypeError, ValueError):
            limit = 30
        if not query:
            return "错误：query 必填"
        res = core.everything_search(query, limit)
        if "error" in res:
            return f"Everything 搜索不可用：{res['error']}"
        if not res["results"]:
            return f"全盘无匹配文件：{query}"
        lines = [f"全盘文件 {len(res['results'])} 个："]
        lines.extend(f"- {p}" for p in res["results"])
        return "\n".join(lines)

    if name == "hub_distill":
        try:
            limit = int(arguments.get("limit", 15))
        except (TypeError, ValueError):
            limit = 15
        cands = brain.distill_candidates(root, limit)
        if not cands:
            return "无蒸馏候选——近期含「目的」的记录都已沉淀或展示过（结晶良好）"
        brain.mark_distill_shown(root, [c["id"] for c in cands])
        lines = [f"记忆蒸馏候选 {len(cands)} 条（记录→记忆的结晶流水线；确认价值后用 "
                 f"hub_memory_write 沉淀，kind 建议 lesson/fact；本轮已登记，之后不再重复推送）："]
        for c in cands:
            lines.append(f"- #{c['id']} [{c['date']} {c['agent']}·{c['project']}] {c['gist'][:70]}")
        return "\n".join(lines)

    if name == "hub_get_record":
        try:
            rid = int(arguments.get("record_id", 0) or 0)
        except (TypeError, ValueError):
            rid = 0
        rec = brain.get_record(root, rid)
        if not rec:
            return f"未找到记录 #{rid}"
        tag = "" if rec["status"] == "active" else f"（status={rec['status']}，已软删/撤销）"
        return (f"records#{rec['id']} [{rec['date']} {rec['agent']}·{rec['project']}]{tag}\n"
                f"标题：{rec['title']}\n\n{rec['content']}")

    if name == "hub_health":
        h = brain.health_report(root)
        lines = ["大脑体检报告：",
                 f"- 规模：记录 {h['records']} 条 · 记忆 {h['memories']} 条 · 项目 {h['projects']}"
                 f"（停滞 {h['projects_stalled']}）· 待处理错误 {h['errors_open']}",
                 f"- 结晶率 {h['crystallization']}%（记忆/记录）· 检索：今日 {h['searches_today']} / 累计 {h['searches_total']} 次",
                 f"- 待办线索 {h['todo_count']} 条 · 疑似重复记忆 {h['dup_memory_count']} 组"]
        if h["todos"]:
            lines.append("待办线索（最近 5 条，全部用 hub_list_todos）：")
            for t in h["todos"][:5]:
                lines.append(f"  · [{t['date']} {t['agent']}·{t['project']}] {t['todo'][:60]}")
        if h["dup_memories"]:
            lines.append("疑似重复记忆（建议合并或按「取代记忆#N」约定处理）：")
            for d in h["dup_memories"][:5]:
                lines.append(f"  · #{d['a']}~#{d['b']} 相似{d['sim']} [{d.get('verdict', '')}]：{d['content_a'][:40]}")
        return "\n".join(lines)

    if name == "hub_get_progress":
        try:
            limit = int(arguments.get("limit", 30))
        except (TypeError, ValueError):
            limit = 30
        limit = max(1, min(limit, 200))
        entries = brain.get_progress(root, limit)
        by_agent: dict = {}
        for r in entries:
            by_agent.setdefault(r["agent"] or "其他", []).append(r)
        lines = ["各 agent 最近工作（对齐进度用）："]
        for a, rs in sorted(by_agent.items()):
            lines.append(f"[{a}]")
            for r in rs[:10]:
                lines.append(f"  {r['date']} {r['project']}：{r['title'][:60]}")
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
    {"name": "hub_search", "description": "跨项目全文搜索工作记录/记忆/文件名（记录行带 #id，判断不了价值先看片段，精读用 hub_get_record）",
     "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]}},
    {"name": "hub_get_rules", "description": "读取团队协作规范（目录命名/记录格式/铁律）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_memory_read", "description": "读取大脑记忆库（结构化：环境事实/用户偏好/踩坑经验/项目进展/随手记）。带 query 可关键词检索，kind 按类型过滤",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "关键词检索，缺省返回置顶+最近概览"},
                                    "kind": {"type": "string", "enum": ["fact", "preference", "lesson", "project", "note"]},
                                    "limit": {"type": "integer"}}}},
    {"name": "hub_memory_write", "description": "写入一条大脑记忆，供所有 agent 检索。kind 建议明确：环境事实用 fact、用户偏好用 preference、踩坑用 lesson、项目进展用 project",
     "inputSchema": {"type": "object",
                     "properties": {"content": {"type": "string"},
                                    "kind": {"type": "string", "enum": ["fact", "preference", "lesson", "project", "note"], "description": "记忆类型，缺省 note"},
                                    "tags": {"type": "string", "description": "标签，逗号分隔"},
                                    "project": {"type": "string", "description": "相关项目，可空"},
                                    "agent": {"type": "string", "description": "你的 agent 名"},
                                    "pinned": {"type": "boolean", "description": "置顶（每次读记忆优先展示）"},
                                    "mode": {"type": "string", "enum": ["append", "overwrite"], "description": "append 新增（默认）；overwrite 按内容匹配更新"}},
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
    {"name": "hub_list_agents", "description": "列出注册 agent 身份表（在线状态/累计记录/心跳次数/最近活跃）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_list_todos", "description": "列出工作记录里的待办/承诺线索（后续/待验证/下一步…句式，元认知）",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "条数，默认20"}}}},
    {"name": "hub_health", "description": "大脑体检报告：规模/结晶率/待办线索/疑似重复记忆/检索活跃度+建议",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_todo_done", "description": "勾销待办线索（处理完的欠账销账，闭环）——传 hub_list_todos 行里的待办串",
     "inputSchema": {"type": "object",
                     "properties": {"record_id": {"type": "integer", "description": "记录 id"},
                                    "todo": {"type": "string", "description": "待办原文"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["todo"]}},
    {"name": "hub_env_set", "description": "登记/更新本机环境配置项（网络/系统/工具/路径…结构化档案）",
     "inputSchema": {"type": "object",
                     "properties": {"category": {"type": "string"}, "key": {"type": "string"},
                                    "value": {"type": "string"}, "agent": {"type": "string"}},
                     "required": ["category", "key", "value"]}},
    {"name": "hub_env_list", "description": "浏览/搜索本机环境配置档案（可按 category 或关键词过滤）",
     "inputSchema": {"type": "object",
                     "properties": {"category": {"type": "string"}, "kw": {"type": "string"},
                                    "limit": {"type": "integer"}}}},
    {"name": "hub_search_files", "description": "Everything 全盘文件名搜索（毫秒级，需 Everything 运行）",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "文件名关键词"},
                                    "limit": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "hub_distill", "description": "记忆蒸馏候选：content 有「目的」结论但同项目无记忆覆盖的记录（结晶流水线，确认后用 hub_memory_write 沉淀）",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer"}}}},
    {"name": "hub_get_record", "description": "读取单条工作记录全文（hub_search/hub_distill 只给摘要；蒸馏候选精读、复盘引用原文用）",
     "inputSchema": {"type": "object",
                     "properties": {"record_id": {"type": "integer", "description": "记录 id，如 2037"}},
                     "required": ["record_id"]}},
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
        except Exception as e:  # noqa: BLE001  工具错误以 isError 返回，不断连，并自动落库错误登记
            text = f"工具执行失败：{type(e).__name__}: {e}"
            try:
                brain.error_add(root, str(args.get("agent") or "mcp-server"),
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


def serve(root: str, fixed: bool = False) -> int:
    """stdio 主循环。EOF/键盘中断退出。
    fixed=False 时每条请求重读 config 的 root：根目录迁移后旧进程自动跟随，
    杜绝"进程启动时缓存旧路径、静默读写幽灵旧库"（2026-09-30 搬家实测踩坑）。"""
    log = lambda s: print(f"[agenthub-mcp] {s}", file=sys.stderr)  # noqa: E731
    log(f"启动，根目录：{root}" + ("（固定）" if fixed else "（动态跟随 config）"))
    err = brain.ensure_schema(root)  # 建表责任前移到 server：代码部署后旧库缺新表也能自愈
    if err:
        log(f"schema 初始化失败：{err}")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        if not fixed:
            live = core.get_root()
            if live and live != root:
                root = live
                log(f"根目录已切换：{root}")
                brain.ensure_schema(root)
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
    fixed = len(sys.argv) > 1  # 命令行显式指定根目录 = 固定模式（向后兼容）
    root = sys.argv[1] if fixed else core.get_root()
    sys.exit(serve(root, fixed))


if __name__ == "__main__":
    main()
