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
import re
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
import ops  # noqa: E402

# 流水落库（brain.db）；文件版仅作兜底
core.JOURNAL_SINK = brain.journal_add

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "agenthub", "version": "2.36.0"}
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05",)  # P3-1 协商集合（dual-era：initialize 握手保留，任意版本回显）
SERVER_PROFILE = "admin"     # P3-1 profile 切片：admin=全量 / worker=日常读写 / reader=最小只读
DEFAULT_MAX_CHARS = 4000     # hub_* 结果默认字符预算；0=不截断；超出截断并在文末标注 + structuredContent
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


# ---------------------------------------------------------------- 进程身份（B1/B2，2026-10-10）

# 本进程所属 agent：由启动参数 --agent 锁定，**进程存活期内不可变**。
# 为什么要有它：接入条目过去只有 {command, args}、六家 agent 启动参数一模一样，
# server 被拉起时不知道自己是谁 → 只能靠工具调用参数里的 agent 自报 → 必然填错。
# 现在身份在**启动那一刻**由接入配置写死（--agent zcode），此后所有工具调用
# 一律以它为准，调用参数里的 agent 只作交叉校验、不再参与判定。
SERVER_AGENT = ""
# 交叉校验留痕节流：同类不一致每进程只记一次（避免刷爆 journal）
_MISMATCH_SEEN: set = set()
# 只读筛选参数白名单：这些工具的 agent 参数是"查别人"用，**不下发**（保持原语义）。
# 其余工具一律下发——身份是"我是谁"，不是"我想查谁"。
# 跨 agent 语义的工具：调用方不传 agent 时意为「全机」，绝不能被进程身份覆盖成只查自己家。
# 踩坑（2026-10-11 实测）：hub_skill_match 漏登记在此 → 被注入 agent='workbuddy'（归一小写），
# 而 skills 表存的是显示名 'WorkBuddy' → WHERE agent=? 恒 0 行 → 五种查询变体一律「技能索引无命中」。
# 新增任何「跨 agent 读取」的工具，都必须同步登记到这里。
_AGENT_FILTER_TOOLS = {"hub_list_skills", "hub_bootstrap_status", "hub_skill_match"}


def apply_server_agent(tname: str, arguments: dict, root: str) -> dict:
    """把进程锁定的身份下发到本次调用（B2 单点派生）。

    一处覆盖全部 agent 消费点——不必逐个工具改，新工具自动受益。
    无 SERVER_AGENT（旧接入条目/手工调用）时原样返回，退回调用参数语义（兼容）。
    自报值与锁定值不符时留一条 journal（B3b 运行期交叉校验）。
    """
    if not SERVER_AGENT or tname in _AGENT_FILTER_TOOLS:
        return arguments
    called = str(arguments.get("agent", "") or "").strip()
    if called and brain._norm_agent(called) != SERVER_AGENT and root:
        key = (called.lower(), SERVER_AGENT)
        if key not in _MISMATCH_SEEN:
            _MISMATCH_SEEN.add(key)
            try:
                core.journal(root, SERVER_AGENT, "agent_mismatch", f"mcp:{tname}",
                             note=f"调用参数自称 {called}，以启动参数 {SERVER_AGENT} 为准")
            except Exception:  # noqa: BLE001 留痕失败不影响主流程
                pass
    out = dict(arguments)  # 不改调用方原对象
    out["agent"] = SERVER_AGENT
    return out


def _agent_for_write(arguments: dict, root: str) -> tuple[str, str]:
    """写入类工具的 agent 取值。返回 (agent, 错误或"")。

    ★ 关键区分（2026-10-10）：**锁定值不校验，自报值才校验**。
      · 锁定值来自接入配置（`--agent <名>`），是**操作者设定**，不是模型编的。
        配错了只该在启动日志里警告——若在这里拒收，会让该 agent 连一条工作都记不了，
        那是比"归属不精确"严重得多的故障。
      · 自报值来自工具调用参数（**模型可控**），才是需要防的填错对象。
    """
    if SERVER_AGENT:
        return SERVER_AGENT, ""
    return brain.check_agent(str(arguments.get("agent", "unknown")), root)


def _guess_agent(root: str) -> str:
    """推断检索者身份：**优先用进程锁定的身份**（B1 起，可靠）；
    无锁定（旧接入/手工调用）才退回"活跃会话"启发式（单会话可靠、多会话加 ? 标注）。"""
    if SERVER_AGENT:
        return SERVER_AGENT
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
    # B2 单点派生：把进程锁定身份下发到本次调用（覆盖 agent 参数）。
    # 放在最前，后面所有工具的 arguments.get("agent") 拿到的都是已校验的锁定值。
    arguments = apply_server_agent(name, arguments, root)

    # ---- P3-1 协议 / 发现 / 审计 / 索引维护（不受 profile 藏起影响，可直接调用）----

    if name == "hub_help":
        vis = visible_tools()
        lines = [f"AgentHub MCP v{SERVER_INFO['version']} —— profile={SERVER_PROFILE}",
                 f"可见工具 {len(vis)}/{len(TOOLS)}（profile 只影响 tools/list 可见面；"
                 "被藏起的工具仍可直接调用）", "", "可见工具："]
        for t in vis:
            lines.append(f"- {t['name']}：{t['description'][:56]}")
        lines += ["", "用法四件事：",
                  "① 发现：hub_search_tools(关键词) 搜全部工具（含藏起）；hub_call_tool(name, arguments) 调任意工具",
                  "② 预演：写工具加 dry_run=true——先看将要写什么，不落库",
                  "③ 确认：写工具传 confirm=false → 返回 input_required（return-and-resume，服务端不阻塞）",
                  f"④ 预算：结果超 max_chars（默认 {DEFAULT_MAX_CHARS}）截断并标注，完整内容在 structuredContent"]
        return "\n".join(lines)

    if name == "hub_search_tools":
        kw = str(arguments.get("keyword", "")).strip().lower()
        hits = [t for t in TOOLS
                if (not kw) or kw in t["name"].lower() or kw in t["description"].lower()]
        visible_names = {t["name"] for t in visible_tools()}
        lines = [f"工具搜索「{kw or '（全部）'}」：{len(hits)} 个命中（含当前 profile 藏起的）"]
        for t in hits[:40]:
            tag = "" if t["name"] in visible_names else " [藏起]"
            lines.append(f"- {t['name']}{tag}：{t['description'][:64]}")
        if len(hits) > 40:
            lines.append(f"…（还有 {len(hits) - 40} 个，收窄关键词再看）")
        lines.append("调用：hub_call_tool(name, arguments)，或直接 tools/call（藏起≠禁用）。")
        return "\n".join(lines)

    if name == "hub_call_tool":
        inner = str(arguments.get("name", "")).strip()
        iargs = arguments.get("arguments") or {}
        if not isinstance(iargs, dict):
            return f"错误：arguments 需为对象（收到 {type(iargs).__name__}）"
        if not inner or inner == "hub_call_tool":
            return "错误：name 必填，且不能是 hub_call_tool 本身（防递归）"
        if inner not in {t["name"] for t in TOOLS}:
            return f"错误：未知工具 {inner}（用 hub_search_tools 搜索可用工具）"
        if inner in WRITE_TOOLS and iargs.get("dry_run") is True:
            return f"dry_run（未执行）：{inner} 预演（经 hub_call_tool）；去掉 dry_run 后正式执行。"
        out = call_tool(inner, iargs, root)
        if inner in WRITE_TOOLS:
            _audit_write(root, inner, iargs, out)
        return out

    if name == "hub_audit":
        try:
            lim = int(arguments.get("limit") or 20)
        except (TypeError, ValueError):
            lim = 20
        rows = brain.audit_list(root, lim)
        if not rows:
            return "暂无写操作审计（P3-1 起，每次写成功都会留痕；dry_run 与 confirm 拦截不计入）"
        lines = [f"最近 {len(rows)} 条写操作审计（倒序）："]
        for r in rows:
            undo = f" · undo={r['undo_kind']}#{r['undo_ref']}" if r.get("undo_ref") else ""
            proj = f" · {r['project']}" if r.get("project") else ""
            lines.append(f"- #{r['id']} {r['ts'][:19]} [{r['agent'] or '-'}] {r['tool']}"
                         f"{proj}：{r['summary'][:70]}{undo}")
        return "\n".join(lines)

    if name == "hub_flush_index":
        res = brain.flush_index(root, force=bool(arguments.get("force")))
        if res.get("error"):
            return "错误：" + res["error"]
        return (f"语义索引已刷新：records +{res.get('records', 0)} / memories +{res.get('memories', 0)}"
                f"（扫描 {res.get('scanned', 0)} 条；model={res.get('model')} · dim={res.get('dim')}）")

    # ---- P3-2 待办状态机 + 事件流 ----

    if name == "hub_task_add":
        err, tid = brain.task_add(root, str(arguments.get("title", "")),
                                  project=str(arguments.get("project", "")),
                                  detail=str(arguments.get("detail", "")),
                                  blocked_by=str(arguments.get("blocked_by", "")),
                                  agent=str(arguments.get("agent", "")),
                                  priority=arguments.get("priority", brain.DEFAULT_PRIORITY),
                                  deps=str(arguments.get("deps", "")),
                                  due=str(arguments.get("due", "")))
        if err:
            return "错误：" + err
        return (f"任务已建 #{tid}：{str(arguments.get('title', ''))[:60]}\n"
                "认领：hub_claim；下一步推荐：hub_next；列表：hub_tasks")

    if name == "hub_claim":
        err, row = brain.task_claim(root, arguments.get("task_id"),
                                    str(arguments.get("agent", "")))
        if err:
            return err
        return (f"认领成功：任务 #{row['id']} [{brain.TASK_CN.get(row['status'], row['status'])}] "
                f"{row['title'][:60]} —— owner={row['owner']}"
                f"（超 {brain.CLAIM_TTL_HOURS}h 未更新自动可被接管）")

    if name == "hub_task_update":
        err, row = brain.task_update(root, arguments.get("task_id"),
                                     status=str(arguments.get("status", "")),
                                     agent=str(arguments.get("agent", "")),
                                     blocked_by=str(arguments.get("blocked_by", "")),
                                     detail=str(arguments.get("detail", "")),
                                     priority=arguments.get("priority"),
                                     deps=str(arguments.get("deps", "")),
                                     due=str(arguments.get("due", "")))
        if err:
            return f"错误：{err}"
        msg = (f"任务 #{row['id']} 已更新：status={brain.TASK_CN.get(row['status'], row['status'])}"
               f" owner={row['owner'] or '-'}｜{row['title'][:50]}")
        if row.get("crystal"):
            msg += f"\n{row['crystal']}"
        return msg

    if name == "hub_tasks":
        try:
            lim = int(arguments.get("limit") or 50)
        except (TypeError, ValueError):
            lim = 50
        rows = brain.task_list(root, status=str(arguments.get("status", "")),
                               owner=str(arguments.get("owner", "")),
                               project=str(arguments.get("project", "")),
                               limit=lim)
        if not rows:
            return "暂无任务（hub_task_add 新建；状态 pending/in_progress/blocked/done）"
        lines = [f"任务 {len(rows)} 条（进行中→阻塞→待办→完成 排序）："]
        for t in rows:
            who = f" @{t['owner']}" if t["owner"] else ""
            blk = (f" 等待:{t['blocked_by'][:30]}"
                   if t["status"] == "blocked" and t["blocked_by"] else "")
            proj = f"{t['project']}｜" if t["project"] else ""
            pri = t.get("priority")
            pri = brain.DEFAULT_PRIORITY if pri is None else int(pri)
            lines.append(f"- #{t['id']} [{brain.TASK_CN.get(t['status'], t['status'])}][P{pri}]"
                         f"{who}{blk} {proj}{t['title'][:60]}")
        lines.append("（下一步推荐：hub_next；认领：hub_claim）")
        return "\n".join(lines)

    if name == "hub_next":
        try:
            lim = int(arguments.get("limit") or 5)
        except (TypeError, ValueError):
            lim = 5
        res = brain.task_next(root, project=str(arguments.get("project", "")),
                              owner=str(arguments.get("agent", "")), limit=lim)
        c = res["counts"]
        if not any(c.values()):
            return ("前沿队列为空（没有待办/在办任务）。\n"
                    "不知道从哪下手时：hub_list_todos 看待办线索，hub_list_projects 看停滞项目。")
        lines = [f"下一步推荐（前沿队列：依赖已满足、可直接认领）共 {c['ready']} 条："]
        for i, t in enumerate(res["ready"], 1):
            proj = f"{t['project']}｜" if t["project"] else ""
            due = f"（截止 {(t.get('due') or '')[:10]}）" if (t.get("due") or "").strip() else ""
            lines.append(f"{i}. #{t['id']} [P{t['_priority']}] {proj}{t['title'][:60]}{due}")
        if res["doing"]:
            items = "；".join(
                f"#{t['id']}[P{t['_priority']}]@{t['owner'] or '-'} {t['title'][:28]}"
                for t in res["doing"][:4])
            lines.append(f"进行中 {c['doing']} 条：{items}")
        if res["takeover"]:
            items = "；".join(
                f"#{t['id']}@{t['owner'] or '-'} {t['title'][:28]}" for t in res["takeover"][:3])
            lines.append(f"可接管（超 {brain.CLAIM_TTL_HOURS}h 未更新）：{items}")
        if res["waiting"]:
            lines.append(f"等待中 {c['waiting']} 条：")
            for t in res["waiting"][:4]:
                w = t.get("_wait") or []
                if w:
                    why = "前置未完成：等 " + "、".join(
                        f"#{x['id']}（{brain.TASK_CN.get(x['status'], x['status'])}）"
                        for x in w[:3])
                else:
                    why = f"阻塞：{(t.get('blocked_by') or '见详情')[:30]}"
                lines.append(f"  - #{t['id']} [P{t['_priority']}] {t['title'][:40]} —— {why}")
        lines.append("（认领开干：hub_claim；全部任务：hub_tasks；任务完成会自动结晶）")
        return "\n".join(lines)

    if name == "hub_crystallize":
        err, summary = brain.task_crystallize(root, arguments.get("task_id"),
                                              force=bool(arguments.get("force")))
        if err:
            return "错误：" + err
        return summary

    if name == "hub_event_write":
        err, eid = brain.event_add(root, str(arguments.get("content", "")),
                                   kind=str(arguments.get("kind", "progress")),
                                   project=str(arguments.get("project", "")),
                                   agent=str(arguments.get("agent", "")),
                                   ref=str(arguments.get("ref", "")))
        if err:
            return "错误：" + err
        return f"事件已写入 #{eid}（其他 agent 用 hub_events(since_id) 增量订阅）"

    if name == "hub_events":
        try:
            since = int(arguments.get("since_id") or 0)
            lim = int(arguments.get("limit") or 30)
        except (TypeError, ValueError):
            since, lim = 0, 30
        rows = brain.event_list(root, since_id=since, limit=lim,
                                project=str(arguments.get("project", "")),
                                agent=str(arguments.get("agent", "")))
        if not rows:
            return "暂无事件（hub_event_write 写入；since_id 增量订阅）"
        lines = [f"事件流 {len(rows)} 条（倒序；增量订阅传 since_id={rows[0]['id']}）："]
        for e in rows:
            proj = f" · {e['project']}" if e["project"] else ""
            lines.append(f"- #{e['id']} {e['ts'][5:16]} [{e['agent'] or '-'}·{e['kind']}]"
                         f"{proj}：{e['content'][:80]}")
        return "\n".join(lines)

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
        # agent 身份校验（2026-10-09）：名不再自由文本。填错会污染时间线归属/过滤/统计，
        # 故在写入前当场拒收并给出可用名单；确无归属时传 unknown（合法）。
        agent, aerr = _agent_for_write(arguments, root)
        if aerr:
            return "错误：" + aerr
        content = str(arguments.get("content", "")).strip()
        # 3.2 AgDR 决策字段（可选）：畸形类型转 str 兜底，空视为未传
        ctx = str(arguments.get("context", "") or "").strip()
        opts = str(arguments.get("options", "") or "").strip()
        dec = str(arguments.get("decision", "") or "").strip()
        tro = str(arguments.get("tradeoffs", "") or "").strip()
        err = _check_content(content, "content")
        if err:
            return err
        if not pname:
            return "错误：project 必填"
        # 决策段拼进正文后整体受 MAX_CONTENT 把守（防超长决策字段绕过校验）；渲染点唯一在 add_record
        if len(content + brain.render_decision(ctx, opts, dec, tro)) > MAX_CONTENT:
            return f"错误：content+决策字段超长（{MAX_CONTENT} 上限），请精简后分条写入"
        # date 畸形兜底：从参数里提取 YYYY-MM-DD，取不到用今天
        dm = core.DATE_RE.search(str(arguments.get("date") or ""))
        date = dm.group(1) if dm else datetime.date.today().isoformat()
        # P1-2 record_type：可选枚举，非法当场拒收（其余字段缺省自动推断）
        rt = str(arguments.get("record_type") or "").strip()
        if rt and rt not in brain.RECORD_TYPES:
            return f"错误：record_type 只能是 {'/'.join(brain.RECORD_TYPES)}（收到 {rt!r}）"
        # 技能原生化·阶段2：硬门禁（主人拍板「直接阻断」）。只拦判得准的检查项——
        # Ponytail 粗筛只提示不拦（包名≠导入名会误报），误伤正常开发比漏拦更贵。
        waive = str(arguments.get("waive", "") or "").strip()
        pdir = Path(root) / pname
        if not waive and pdir.is_dir():
            try:
                g = ops.run_check("coverage_gate", str(pdir), root)
            except Exception:  # noqa: BLE001
                g = {"ok": True, "summary": "", "detail": []}  # 门禁自身故障绝不阻断正常工作
            if g and not g.get("ok"):
                detail = "\n".join("  - " + d for d in (g.get("detail") or [])[:8])
                return ("⛔ 门禁未通过（coverage_gate 分片对账），本次记录未写入：\n"
                        f"{g.get('summary', '')}\n{detail}\n"
                        "处理：① 补查/修复后重交 ② 确属正常缩水 → 本次调用带 waive=\"<理由>\" 显式豁免"
                        "（豁免写进记录正文，事后可查）")
        if waive:  # 豁免留痕：可追溯的显式动作，比静默失败强
            tag = f"\n[豁免门禁] {waive[:100]}"
            room = MAX_CONTENT - len(tag)
            content = (content[:room] if len(content) > room else content) + tag
        rec_id = brain.add_record(root, pname, agent, date, _auto_title(content, date, agent), content,
                                  context=ctx, options=opts, decision=dec, tradeoffs=tro,
                                  record_type=rt,
                                  review_date=str(arguments.get("review_date", "")).strip()[:10])
        core.journal(root, agent, "log_work", f"brain:records#{rec_id}", note=f"{date}（{agent}）")
        herr, active = brain.heartbeat_touch(root, agent, pname)
        warn = _conflict_warn(active)
        out = f"已记入大脑（records#{rec_id}，项目 {pname}）{warn}"
        # 写入时踩坑拦截：新记录与库内 lesson/fact 相似 → 当场弹出核对
        # （"大脑拦住重复踩坑"的落地时机——比开工推送更贴近踩坑瞬间）
        try:
            hits = brain.similar_lessons_for(root, content)
        except Exception:  # noqa: BLE001
            hits = []
        if hits:
            brain.mark_intercept_hit(root, agent, rec_id, hits)  # 落痕：流水+use_count，验收②有据可查
            out += f"\n⚠️ 大脑拦截提醒：库内已有 {len(hits)} 条相似踩坑/环境事实，动手前先核对："
            for hk in hits:
                tag = (f"错误登记#{-hk['id']}" if hk["id"] < 0 else f"记忆#{hk['id']}")
                out += (f"\n  [{tag}]（{brain.KIND_CN.get(hk['kind'], hk['kind'])}"
                        f"·相似度{hk['sim']}）{hk['content']}")
        try:
            out += ops.wakeups(root, pname)  # 收尾建议：py_compile/git_status/蒸馏候选，当天已办即静默
        except Exception:  # noqa: BLE001
            pass
        return out

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
        n_total = (len(res["records"]) + len(res["memories"]) + len(res["files"])
                   + len(res.get("notes", [])))  # notes 漏加=纯随记命中误报无结果（实测抓到）
        brain.log_search(root, "hub_search", kw, n_total, agent=_guess_agent(root))
        if not n_total:
            return (f"无结果：{kw}\n"
                    "（可试：① 拆成更短的词重查（按子串匹配）② hub_list_projects 看项目名 "
                    "③ hub_memory_read 查记忆 ④ hub_env_list 查环境档案）")
        words = [w for w in kw.split() if w][:8]
        lines = [f"全脑检索「{kw}」：命中 记录{len(res['records'])} · 记忆{len(res['memories'])} · "
                 f"随记{len(res.get('notes', []))} · 文件{len(res['files'])}"
                 f"（记录行带 #id，精读用 hub_get_record）"]
        if any("_bigram" in r for r in res["records"]) or any("_bigram" in mem for mem in res["memories"]):
            lines.insert(1, "（连续长串无直接命中，已拆词放宽召回——按相关度排序，弱相关自行取舍）")
        for r in res["records"]:
            lines.append(f"[记录#{r['id']}] {r['date']} {r['project']}（{r['agent']}）：{r['title'][:60]}")
            snip = _snippet(r["content"], words)
            if snip:
                lines.append(f"  ↳ {snip}")
        for mem in res["memories"]:
            lines.append(f"[记忆#{mem['id']}] {brain.KIND_CN.get(mem['kind'], mem['kind'])}：{mem['content'][:100]}")
        for n in res.get("notes", []):
            mood = f"（{n['mood']}）" if n.get("mood") else ""
            lines.append(f"[随记#{n['id']}] {n['ts'][:10]}{mood}：{n['content'][:100]}")
        for f in res["files"]:
            lines.append(f"[文件] {f['project']}\\{f['name']}")
        if len(lines) > 41:  # 头部 1 行 + 内容 40 行
            lines = lines[:41] + ["（结果较多仅显示前 40 行，换更具体的关键词可缩小范围）"]
        return "\n".join(lines)

    if name == "hub_note":
        content = str(arguments.get("content", "")).strip()
        if not content:
            return "错误：content 必填——人说的原话/随记内容"
        try:
            nid = brain.add_note(
                root, content,
                tags=str(arguments.get("tags", "")).strip(),
                link=str(arguments.get("link", "")).strip(),
                mood=str(arguments.get("mood", "")).strip(),
                source="human" if str(arguments.get("source", "human")) != "web" else "web")
        except ValueError as e:
            return f"错误：{e}"
        core.journal(root, _guess_agent(root), "note_append", f"brain:notes#{nid}")
        mood = f"（{arguments.get('mood')}）" if arguments.get("mood") else ""
        return (f"已记入随记 #{nid}{mood}——人的思想已进大脑，所有 agent 检索可见；"
                f"GUI「随记」页随时回看")

    if name == "hub_note_read":
        nid = str(arguments.get("note_id", "")).strip()
        try:
            n = brain.get_note(root, int(nid))
        except (TypeError, ValueError):
            return "错误：note_id 必须是数字（如 12）"
        if not n:
            return f"错误：随记 #{nid} 不存在（或为未到期的时光胶囊，对检索隐藏）"
        mood = f"（{n['mood']}）" if n["mood"] else ""
        tags = f" 标签:{n['tags']}" if n["tags"] else ""
        ctx = f" 记于「{n['context']}」时" if n["context"] else ""
        star = "★已收藏 " if n["starred"] else ""
        return (f"随记 #{n['id']} {star}{n['ts'][:10]}{mood}{tags}{ctx}\n"
                f"{n['content']}\n"
                f"（深挖协议：向主人反问一个最能展开此想法的问题；"
                f"整理协议：若主人要求，提炼成记忆用 hub_memory_write 写入）")

    if name == "hub_get_rules":
        return core.load_rules(root)

    if name == "hub_memory_read":
        query = str(arguments.get("query", "")).strip()
        kind = str(arguments.get("kind", "")).strip()
        try:
            limit = int(arguments.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        rows = brain.search_memories(root, query, kind, limit, count_hits=bool(query))
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
                    lines = [msg, "记忆之外，工作记录里可能有相关线索（弱相关线索自行取舍，"
                                  "hub_get_record #id 精读）："]
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

    if name == "hub_ask":
        question = str(arguments.get("question", "")).strip()
        project = str(arguments.get("project", "")).strip()
        res = brain.ask(root, question, project, agent=_guess_agent(root))
        brain.log_search(root, "hub_ask", question[:120], len(res["citations"]),
                         agent=_guess_agent(root))
        head = f"问大脑｜意图：{res['intent']}｜置信：{res['confidence']}"
        if res["abstained"]:
            return f"{head}\n{res['answer']}"
        lines = [head, res["answer"], "", "出处："]
        for c in res["citations"][:8]:
            tag = f"[{c['kind']}]" if c.get("kind") else ""
            dt = f" {c['date']}" if c.get("date") else ""
            lines.append(f"· {c['type']}#{c['id']} {tag}{dt}")
        lines.append(f"（{res['note']}；改用 hub_memory_read / hub_get_record #id 精读）")
        return "\n".join(lines)

    if name == "hub_metacognition":
        mc = brain.metacognition(root)
        g = mc["governance"]
        lines = ["大脑自省（元认知 D10）——我记得什么 / 忘了什么 / 可能记错了什么：",
                 f"· 现行记忆 {g['live']}（取代 {g['superseded']} / 过期 {g['expired']} / 作废 {g['retired']}）"
                 f"，证据覆盖 {g['evidence_coverage']:.0%}，平均置信 {g['avg_confidence']}",
                 f"· 元信息占比 {mc['meta_ratio']}% · 疑似矛盾对 {mc['dup_pairs']}"]
        if mc["coverage_blindspots"]:
            lines.append("· 覆盖盲区（有记录、零记忆，最该沉淀）：")
            for b in mc["coverage_blindspots"][:5]:
                lines.append(f"   - {b['project']}（{b['records']} 记录 / {b['memories']} 记忆）")
        if mc["low_confidence"]:
            lines.append("· 低置信现行记忆（可能记错，待复核）：")
            for m in mc["low_confidence"][:5]:
                lines.append(f"   - #{m['id']} [{m.get('kind')}] conf={m.get('confidence')} "
                             f"{(m.get('content') or '')[:60]}")
        if mc["pending_decisions"]:
            lines.append("· 久未复核的决策（>30 天，验证回路未建）：")
            for d in mc["pending_decisions"][:5]:
                lines.append(f"   - {d['date']} {d['project']}「{(d.get('title') or '')[:30]}」"
                             f"（记录#{d['id']}）")
        lines.append("（大脑主动承认哪里不靠谱——盲区/低置信/矛盾/待复核；只读）")
        return "\n".join(lines)

    if name == "hub_memory_write":
        content = str(arguments.get("content", "")).strip()
        mode = str(arguments.get("mode", "append"))
        kind = str(arguments.get("kind", "note")).strip()
        tags = str(arguments.get("tags", "")).strip()
        project = str(arguments.get("project", "")).strip()
        # agent 身份校验，同 hub_log_work（记忆归属错会误导推送与统计）
        agent, aerr = _agent_for_write(arguments, root)
        if aerr:
            return "错误：" + aerr
        pinned = bool(arguments.get("pinned", False))
        # 批次 A：记忆取代 / 双时相 / 置信度 / 证据链
        try:
            supersedes = int(arguments.get("supersedes", 0) or 0)
        except (TypeError, ValueError):
            supersedes = 0
        valid_until = str(arguments.get("valid_until", "")).strip()[:10]
        try:
            confidence = float(arguments.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        evidence = str(arguments.get("evidence", "")).strip()
        # M6 成熟度（信任分层）：新记忆生来 draft；写入者已确认过的结论才标 validated
        maturity = str(arguments.get("maturity", "draft")).strip()
        if maturity not in brain.MATURITY_LEVELS:
            maturity = "draft"
        err = _check_content(content, "content")
        if err:
            return err
        # 反馈通道（0.4）：feedback+memory_id 同时给出时改走反馈分支，不新增记忆
        fb = str(arguments.get("feedback", "")).strip()
        mid_arg = arguments.get("memory_id")
        if fb and mid_arg is not None:
            try:
                return brain.set_memory_feedback(root, int(mid_arg), fb)
            except (TypeError, ValueError):
                return f"memory_id 非法：{mid_arg!r}（应为正整数）"
        if mode not in ("append", "overwrite", "new"):
            return "错误：mode 只能是 append/new 或 overwrite"
        if mode == "overwrite":
            # overwrite 语义：按内容精确匹配更新旧条目（v1 文本时代遗留接口），否则当新条目
            # count_hits=False：写入查重是内部调用，计唤起会让刚写的记忆立刻 +1，污染数据
            rows = brain.search_memories(root, content[:80], "", 1, count_hits=False)
            if rows and rows[0]["content"] == content:
                brain.edit_memory(root, rows[0]["id"], content=content,
                                  kind=kind if kind in brain.KINDS else "", agent=agent)
                core.journal(root, agent, "memory_overwrite", f"brain:memories#{rows[0]['id']}")
                return f"已更新大脑记忆 #{rows[0]['id']}"
        mid = brain.add_memory(root, content, kind, tags, project, agent, pinned,
                              supersedes=supersedes, valid_until=valid_until,
                              confidence=confidence, evidence=evidence,
                              maturity=maturity)
        core.journal(root, agent, "memory_append", f"brain:memories#{mid}", note=kind)
        extra = ""
        if supersedes > 0:
            extra += f"，已取代 #{supersedes}（旧条退出推送但仍可追溯）"
        if valid_until:
            extra += f"，有效期至 {valid_until}"
        if evidence:
            extra += f"，证据链 {evidence}"
        return f"已写入大脑记忆 #{mid}（{brain.KIND_CN.get(kind, kind)}），所有 agent 可读可检索{extra}"

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
                recall = brain.recall_for(root, project, agent=agent)
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
            if recall:  # 先把记忆推送定型，技能建议再追加（顺序反了会被 lines 覆盖掉）
                out = "\n".join(lines)
        # 技能原生化·阶段4：开工顺带召回本任务可能用到的技能（跨 agent，只给路径不加载）。
        # 独立于记忆推送的免推窗口——任务换了（note 变了）就该重新给，不该被记忆的免推连坐。
        # 硬门槛（主人明确反感元信息占位）：① note 为空或没变不推 ② 最多 3 条
        # ③ 低于「top1 的 35% 且绝对分 <3」的一律不推——宁缺毋滥，推错比不推更烦。
        if note:
            key = (agent or "", project or "")
            if _LAST_SKILL_NOTE.get(key) != note:  # 进程内去重：同一任务不重复灌
                _LAST_SKILL_NOTE[key] = note
                try:
                    sk = brain.skill_match(root, note, topk=3)
                except Exception:  # noqa: BLE001
                    sk = []
                if sk:
                    top1 = sk[0].get("_bm25", 0)
                    sk = [s for s in sk if s.get("_bm25", 0) >= max(3.0, top1 * 0.35)][:3]
                if sk:
                    extra = ["", "🧩 本任务可能用得上的技能（大脑召回，未加载，需要再读文件）："]
                    for s in sk:
                        extra.append(f"- {s['name']} [{s['agent']}] {(s['desc'] or '')[:60]}")
                        extra.append(f"    {s['path']}")
                    for s in sk:
                        brain.skill_hit(root, s["agent"], s["path"])
                    out = out + "\n".join(extra)
        # 主人最新随记（开工视野）：agent 开工即看见主人最近在想什么——双向融入
        try:
            latest = brain.latest_notes_for_agent(root, k=2)
        except Exception:
            latest = []
        if latest:
            nl = ["", "[主人最新随记] 也许与你正要做的事有关（hub_note_read 可读全文）："]
            for n in latest:
                mood = f"（{n['mood']}）" if n["mood"] else ""
                nl.append(f"· #{n['id']} {n['ts'][:10]}{mood} {n['content'][:80]}")
            out = out + "\n".join(nl)
        # M10 工作台：开工即见「下一步该干什么」——前沿队列摘要（hub_next / GUI 工作台同源）。
        # 按本项目过滤；本项目四段全空则回退全局（不漏别处的活）。只在有 ready/doing/takeover
        # 时追加（全空=没有可干的事，不刷噪音）；异常降级不打断心跳。
        try:
            nxt = brain.task_next(root, project=project, limit=3)
            if not any(nxt["counts"][k] for k in ("ready", "doing", "takeover")):
                nxt = brain.task_next(root, limit=3)
        except Exception:
            nxt = None
        if nxt and any(nxt["counts"][k] for k in ("ready", "doing", "takeover")):
            c = nxt["counts"]
            tl = ["", "[今日任务] " + " · ".join(
                f"{brain.TASK_GROUP_CN[k]} {c[k]}"
                for k in ("ready", "doing", "takeover", "waiting") if c[k])]
            for t in nxt["ready"][:3]:
                proj = f"{t['project']}｜" if t["project"] else ""
                tl.append(f"  ▶ #{t['id']} [P{t['_priority']}] {proj}{t['title'][:50]}")
            for t in nxt["doing"][:2]:
                line = f"  ● #{t['id']} [P{t['_priority']}] 进行中 @{t['owner'] or '?'} {t['title'][:40]}"
                try:  # M4：doing 行附最新检查点——进行中的活走到哪了一眼看（断点续跑入口）
                    cp = brain.checkpoint_latest(root, t["id"])
                except Exception:  # noqa: BLE001
                    cp = None
                if cp:
                    line += f"（最新进展：{cp['content'][:40]}）"
                tl.append(line)
            if c["takeover"]:
                tl.append(f"  ⚑ 可接管 {c['takeover']} 条（超时无人动，hub_tasks 查看）")
            tl.append("  选活 hub_next · 认领 hub_claim · 干完标 done 自动结晶")
            out = out + "\n".join(tl)
        # M3 未读信号（开工即见别人@你的消息）：有未读才追加，top2 摘要；异常降级不打断心跳
        try:
            n_un = brain.signal_unread_count(root, agent)
        except Exception:
            n_un = 0
        if n_un:
            try:
                inbox = brain.signal_inbox(root, agent, unread_only=True, limit=2)
            except Exception:
                inbox = []
            sl = ["", f"[未读信号] {n_un} 条（hub_signals 查收 · hub_signal_read 标已读）："]
            for s in inbox:
                to = f"→{s['to_agent']}" if s["to_agent"] else "→广播"
                sl.append(f"  ● #{s['id']} {s['ts'][5:16]} 来自 {s['from_agent']}{to}：{s['content'][:60]}")
            out = out + "\n".join(sl)
        # M11 待验证决策（开工推送第三段）：到期未回顾的决策推出来——决策写了没人回顾=白写
        try:
            due = brain.decision_review_due(root, limit=2)
        except Exception:  # noqa: BLE001
            due = []
        if due:
            dl = ["", f"[待验证决策] {len(due)} 条到期未回顾"
                       "（hub_decision_review action=list 看全部，review 记结论）："]
            for d in due:
                dl.append(f"  ◆ #{d['record_id']} {d['review_date']} 到期｜{d['title'][:36]}"
                          f"｜当时决策：{d['decision_brief'][:40]}")
            out = out + "\n".join(dl)
        # M9 关注项目动态（开工推送第四段）：关注的其他项目近 7 天有进展就速览一条——
        # 不开工在那个项目也知道；排除当前项目（避免与「今日任务」重复刷）
        try:
            vr = brain.view_recent(root, agent, exclude_project=project, limit=2)
        except Exception:  # noqa: BLE001
            vr = []
        if vr:
            vl = ["", "[关注项目动态] 你关注的其他项目最近有进展："]
            for r in vr:
                vl.append(f"  ○ [{r['project']}] {r['title'][:36]}"
                          f"（{r['date']} {r['agent'] or '?'}）")
            vl.append("  管理关注：hub_view action=follow/unfollow/list")
            out = out + "\n".join(vl)
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

    if name == "hub_archive_project":
        agent = str(arguments.get("agent", "unknown")).strip() or "unknown"
        pname = str(arguments.get("project", "")).strip()
        err = brain.archive_project(root, pname, agent)
        if err:
            return f"错误：{err}" if err.startswith(("未找到", "project")) else f"警告：{err}"
        core.journal(root, agent, "archive_project", pname)
        return (f"项目已归档：{pname}（状态=archived，目录已移入 99_Archive\\；"
                f"回滚=目录移回 + 状态改回 active）")

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

    if name == "hub_skill_match":
        q = str(arguments.get("query", "")).strip()
        if not q:
            return "错误：query 必填（一句话描述你这次要干什么，如「留学选校」「抓B站字幕」）"
        try:
            topk = max(1, min(int(arguments.get("topk", 3) or 3), 20))
        except (TypeError, ValueError):
            topk = 3
        want_agent = str(arguments.get("agent", "")).strip()
        hits = brain.skill_match(root, q, topk=topk, agent=want_agent)
        brain.log_search(root, "hub_skill_match", q, len(hits), agent=_guess_agent(root))
        if not hits:
            return (f"技能索引无命中：{q}\n"
                    "（① 索引可能还没导入 → hub_skill_sync ② 换成更短的词 "
                    "③ hub_list_skills 看全量）")
        lines = [f"技能召回「{q}」top{len(hits)}（跨 agent）："]
        for h in hits:
            lines.append(f"- {h['name']} [{h['agent']}] score={h['_bm25']}")
            lines.append(f"    {(h['desc'] or '')[:90]}")
            lines.append(f"    {h['path']}")
        for h in hits:  # 命中回写（统计用，失败静默）
            brain.skill_hit(root, h["agent"], h["path"])
        return "\n".join(lines)

    if name == "hub_skill_sync":
        rows = arguments.get("rows")
        if not rows:
            infos = agentscore.detect_agents(core.load_config().get("extra_agents"))
            rows = [{"name": s.name, "agent": a.name, "path": s.path, "desc": s.desc}
                    for a in infos for s in a.skills]
        only = str(arguments.get("agent", "")).strip().lower()
        if only:
            rows = [r for r in rows if only in str(r.get("agent", "")).lower()]
        stat = brain.skill_sync(root, rows)
        return (f"技能索引同步：新增 {stat['added']} · 更新 {stat['updated']} · "
                f"未变 {stat['unchanged']}（本次提交 {len(rows)} 条）")

    if name == "hub_asset_find":
        kw = str(arguments.get("keyword", "")).strip()
        if not kw:
            return "错误：keyword 必填（软件名或用途，如：录屏、远程控制、压缩）"
        try:
            lim = max(1, min(int(arguments.get("limit", 8) or 8), 30))
        except (TypeError, ValueError):
            lim = 8
        items = brain.asset_find(root, kw, limit=lim)
        if not items:
            return (f"台账无命中：{kw}\n"
                    "（台账只有已收录的软件；装之前仍应以官方源为准，第三方网盘下载自行校验哈希）")
        lines = [f"软件台账「{kw}」{len(items)} 条："]
        for it in items:
            lic = f" · {it['license']}" if it.get("license") else ""
            lines.append(f"- {it['name']} [{it.get('category') or '未分类'}]{lic}")
            lines.append(f"    {(it.get('desc') or '')[:70]}")
            if it.get("website"):
                lines.append(f"    {it['website']}")
        return "\n".join(lines)

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

    if name == "hub_bootstrap_status":
        want = str(arguments.get("agent", "")).strip().lower()
        rows = core.agent_bootstrap_status(root)
        if want:
            rows = [r for r in rows if want in r["agent"].lower()]
        if not rows:
            return f"未找到 agent：{want}"
        lines = [f"agent 接入状态（当前引导块版本 v{core.BOOTSTRAP_VERSION}；引导=规则已注入 / MCP=已配 agenthub）："]
        for r in rows:
            if r.get("boot_stale"):
                boot = f"⚠旧v{r.get('boot_version') or '?'}"
            elif r["boot"]:
                boot = f"✓v{r.get('boot_version') or '?'}"
            else:
                boot = "✗"
            mcp = "—" if r["mcp"] is None else ("✓" if r["mcp"] else "✗")
            lines.append(f"- {r['agent']}  [{r['channel']}]  引导:{boot}  MCP:{mcp}  {r['instr_path']}")
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
            mark = "[元信息?] " if c.get("meta") else ""
            lines.append(f"- #{c['id']} [{c['date']} {c['agent']}·{c['project']}] {mark}{c['gist'][:70]}")
        return "\n".join(lines)

    if name == "hub_get_record":
        try:
            rid = int(arguments.get("record_id", 0) or 0)
        except (TypeError, ValueError):
            rid = 0
        level = str(arguments.get("level", "full") or "full").strip()
        rec = brain.get_record(root, rid, level=level)
        if not rec:
            return f"未找到记录 #{rid}"
        tag = "" if rec["status"] == "active" else f"（status={rec['status']}，已软删/撤销）"
        lv = f" · level={level}" if level != "full" else ""
        return (f"records#{rec['id']} [{rec['date']} {rec['agent']}·{rec['project']}]{tag}{lv}\n"
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
        if h.get("push_by_agent"):
            cov = "、".join(f"{k} {v}次" for k, v in h["push_by_agent"].items())
            lines.append(f"- 记忆推送累计 {h.get('recall_push_total', 0)} 次（{cov}）；"
                         f"主动检索分布 {'、'.join(f'{k} {v}次' for k, v in h.get('search_by_agent', {}).items())}")
        hn = h.get("honest", {})
        if hn:
            lines.append(f"- 诚实指标：记忆有用率 {hn['memory_use_rate']}%"
                         f"（{hn['memories_used']}/{hn['memories_total']} 条被唤起过）"
                         f"· 元信息占比 {hn['meta_ratio']}%"
                         f"· 读写比 {hn['read_write_ratio']}:1")
            lines.append(f"    （口径：{hn['note']}）")
        if h.get("top_pushed"):
            lines.append("- 唤起最多的记忆 top（推送反射弧记数，验收看工作知识占比）：")
            for m in h["top_pushed"][:5]:
                lines.append(f"    #{m['id']}（{m['use_count']}次）[{brain.KIND_CN.get(m['kind'], m['kind'])}] {m['content'][:50]}")
        acc = h.get("acceptance", {})
        if acc:
            cross = "、".join(acc["cross_agent_searches"]) or "无"
            lines.append(f"- 验收达成度（2026-10-14 复查）：①跨agent检索 [{cross}] "
                         f"②拦截命中 {acc['intercept_hits']} 次 ③stalled 使用 {acc['stalled_used']} 次 "
                         f"④置顶位工作知识 {acc['pinned_work']}/{acc['pinned_total']}")
        if h.get("stale_memories"):
            lines.append("记忆保鲜提醒（lesson/fact 超 90 天未唤起，环境可能已漂移——确认仍有效或更新/归档）：")
            for m in h["stale_memories"][:5]:
                lines.append(f"  · #{m['id']} [{brain.KIND_CN.get(m['kind'], m['kind'])}] {m['content'][:50]}")
        return "\n".join(lines)

    if name == "hub_user_profile":
        p = brain.user_profile(root)
        tb, total = p["time_bands"], max(1, p["time_sample"])
        lines = ["用户画像（零 LLM 实测统计+记忆原文引用，无推断结论）：",
                 f"- 活跃时段（样本=操作流水 {p['time_sample']} 条）："
                 + "、".join(f"{k}{v}({v * 100 // total}%)" for k, v in tb.items() if v),
                 f"- 记录分工（共 {p['records']} 条）："
                 + "、".join(f"{k} {v}" for k, v in p["agents"].items())
                 + f"；单日项目占比 {p['oneday_pct']}%"]
        if p["stack"]:
            lines.append("- 技术栈词频 top：" + "、".join(f"{w}×{n}" for w, n in p["stack"]))
        if p["lessons"]:
            lines.append("- 用户纠正史 top（lesson 记忆按唤起排，开工对齐避免重蹈）：")
            for m in p["lessons"][:6]:
                lines.append(f"  · #{m['id']}（{m['use_count']}次）{m['content'][:64]}")
        if p["preference_n"] == 0:
            lines.append("⚠ preference 类记忆 0 条——用户偏好仍埋在 lesson/记录里，"
                         "蒸馏时把「用户怎么工作」的结论沉淀为 preference 类")
        return "\n".join(lines)

    if name == "hub_duplicates":
        try:
            ml = int(arguments.get("min_len", 80))
        except (TypeError, ValueError):
            ml = 80
        projs = brain.detect_duplicate_projects(root)
        recs = brain.detect_duplicate_records(root, min_len=max(0, ml))
        mig = brain.migration_anchor(root)
        s = brain.stats(root)
        lines = [f"数据完整性检测（只读）：重复项目目录 {len(projs)} 组 · "
                 f"逐字重复记录 {len(recs)} 组 · 记录总数 {s['records']}"]
        if mig.get("migrated_at"):
            lines.append(f"口径：迁移存量 {mig['migrated']} 条（{mig['migrated_at']} 一次性导入）"
                         f"· AgentHub 时代新增 {mig['era_new']} 条——跨口径对比无意义")
        for g in projs[:10]:
            lines.append(f"  · 「{g['canonical']}」共 {g['total']} 条 ← " +
                         "、".join(f"{m} {n}条" for m, n in g["counts"].items()))
        if len(projs) > 10:
            lines.append(f"  …另有 {len(projs) - 10} 组")
        lines.append("合并须人工确认：源码层调用 brain.merge_duplicate_projects(root, canonical, "
                     "aliases, confirm=True)（只改记录/记忆归属，保留 agent 字段，可 hub_undo 追溯）；"
                     "文件系统目录另行归档（hub_archive_project）")
        return "\n".join(lines)

    if name == "hub_handoff":
        action = str(arguments.get("action", "list")).strip()
        agent = str(arguments.get("agent", "")).strip()
        if action == "create":
            pname = str(arguments.get("project", "")).strip()
            done = str(arguments.get("done", "")).strip()
            nxt = str(arguments.get("next_step", "")).strip()
            excluded = str(arguments.get("excluded", "")).strip()
            err = brain.create_handoff(root, pname, agent, done, nxt, excluded)
            return err if err.startswith("已创建") else f"错误：{err}"
        if action == "claim":
            try:
                hid = int(arguments.get("handoff_id", 0) or 0)
            except (TypeError, ValueError):
                hid = 0
            err = brain.claim_handoff(root, hid, agent)
            return err if err.startswith("已认领") else f"错误：{err}"
        # list（缺省）：只给 open；带 project 过滤；include_claimed=1 看全部
        rows = brain.list_handoffs(root, str(arguments.get("project", "")).strip(),
                                   bool(arguments.get("include_claimed", False)))
        if not rows:
            return "（无未认领的交接。换 agent 接手前先 hub_handoff(action=create) 留下上下文）"
        lines = [f"交接清单（{'全部' if arguments.get('include_claimed') else 'open'}，新在前）{len(rows)} 条："]
        for r in rows:
            mark = f"（已被 {r['claimed_by']} 认领）" if r["status"] != "open" else "（待认领）"
            lines.append(f"- #{r['id']} [{r['project']}] 来自 {r['agent']} {mark}")
            if r["done"]:
                lines.append(f"  做完：{r['done'][:80]}")
            if r["next_step"]:
                lines.append(f"  下一步：{r['next_step'][:80]}")
            if r["excluded"]:
                lines.append(f"  已排除：{r['excluded'][:80]}")
        return "\n".join(lines)

    if name == "hub_signal_send":
        tip, sid = brain.signal_send(root, str(arguments.get("agent", "")),
                                     str(arguments.get("content", "")),
                                     to_agent=str(arguments.get("to_agent", "")),
                                     kind=str(arguments.get("kind", "msg")),
                                     project=str(arguments.get("project", "")))
        if not sid:
            return f"错误：{tip}"
        return tip + "\n对方用 hub_signals 查收；未读时其下次心跳会自动提醒"

    if name == "hub_signals":
        rows = brain.signal_inbox(root, str(arguments.get("agent", "")),
                                  bool(arguments.get("unread_only", False)))
        if not rows:
            return ("（无未读信号）" if arguments.get("unread_only")
                    else "（收件箱空。hub_signal_send 给其他 agent 发消息；to_agent 留空=广播）")
        n_unread = sum(1 for r in rows if not r["_read"])
        lines = [f"信号 {len(rows)} 条（未读 {n_unread}，新在上）："]
        for r in rows:
            to = f"→{r['to_agent']}" if r["to_agent"] else "→广播"
            dot = "·" if r["_read"] else "●"
            proj = f" [{r['project']}]" if r["project"] else ""
            lines.append(f"{dot} #{r['id']} {r['ts'][5:16]} {r['from_agent']}{to}{proj}"
                         f"（{r['kind']}）{r['content'][:80]}")
        lines.append("标已读：hub_signal_read(signal_id, agent)")
        return "\n".join(lines)

    if name == "hub_signal_read":
        return brain.signal_mark_read(root, arguments.get("signal_id"),
                                      str(arguments.get("agent", "")))

    if name == "hub_checkpoint":
        action = str(arguments.get("action", "save")).strip()
        tid = arguments.get("task_id")
        if action == "save":
            tip, cid = brain.checkpoint_save(root, tid, str(arguments.get("agent", "")),
                                             str(arguments.get("content", "")),
                                             kind=str(arguments.get("kind", "manual")))
            if not cid:
                return f"错误：{tip}"
            return (f"检查点已存 #{cid}\n"
                    "长任务建议每完成一小步就存一条——断点续跑/换人接手不用从零翻记录")
        if action == "list":
            rows = brain.checkpoint_list(root, tid)
            if not rows:
                return f"（任务 #{tid} 还没有检查点。执行中随手 hub_checkpoint(action=save, content=...)）"
            lines = [f"检查点时间线 {len(rows)} 条（新在上）："]
            for c in rows:
                tag = "自动" if c["kind"] == "auto_claim" else "手写"
                lines.append(f"- #{c['id']} {c['ts'][5:16]} [{tag}] {c['agent']}：{c['content'][:80]}")
            return "\n".join(lines)
        cp = brain.checkpoint_latest(root, tid)
        if not cp:
            return f"（任务 #{tid} 还没有检查点）"
        return f"任务 #{tid} 最新检查点（{cp['ts'][5:16]} {cp['agent']}）：{cp['content']}"

    if name == "hub_sentry":
        summary, sid = brain.sentry_sweep(root)
        return summary + ("\n（已广播给所有 agent——下次开工即在心跳里可见）" if sid else "")

    if name == "hub_quarantine":
        action = str(arguments.get("action", "list")).strip()
        agent = str(arguments.get("agent", "")).strip()
        target = str(arguments.get("target", "record")).strip()
        if action in ("quarantine", "release"):
            if target == "memory":
                if action == "quarantine":
                    return brain.quarantine_memory(root, arguments.get("id"), agent,
                                                   str(arguments.get("reason", "")))
                return brain.release_memory(root, arguments.get("id"), agent)
            if action == "quarantine":
                return brain.quarantine_record(root, arguments.get("id"), agent,
                                               str(arguments.get("reason", "")))
            return brain.release_record(root, arguments.get("id"), agent)
        rows = brain.quarantine_list(root)
        if not rows:
            return ("（隔离区空。发现可疑内容：hub_quarantine action=quarantine "
                    "target=record|memory——退出检索但不删除，可解除）")
        lines = [f"隔离区 {len(rows)} 条（退出检索与推送，未删除）："]
        for r in rows:
            if r["src"] == "record":
                lines.append(f"- 记录 #{r['id']} [{r['project'] or '无项目'}] {r['title']}")
            else:
                lines.append(f"- 记忆 #{r['id']} [{r['kind']}] {r['content']}")
        lines.append("解除：hub_quarantine action=release target=… id=…")
        return "\n".join(lines)

    if name == "hub_decision_review":
        action = str(arguments.get("action", "list")).strip()
        if action == "review":
            return brain.decision_review(root, arguments.get("review_id"),
                                         str(arguments.get("verdict", "")),
                                         str(arguments.get("note", "")),
                                         str(arguments.get("agent", "")))
        if action == "history":
            rows = brain.decision_review_history(root)
            if not rows:
                return "（还没有已回顾的决策。写决策时带 review_date，到期即可回顾）"
            vcn = {"right": "✔ 对", "wrong": "✘ 错", "partial": "◐ 部分对"}
            lines = [f"决策回顾史 {len(rows)} 条（新在前，错过的决策比对的更有复盘价值）："]
            for h in rows:
                lines.append(f"- #{h['id']} [{vcn.get(h['verdict'], h['verdict'])}] "
                             f"{h['review_date']}｜{h['title'][:44]}"
                             + (f"——{h['note'][:50]}" if h["note"] else ""))
            return "\n".join(lines)
        rows = brain.decision_review_due(root)
        if not rows:
            return ("（无到期待验证的决策。写决策记录时传 review_date=YYYY-MM-DD，"
                    "到期会自动出现在这里和心跳开工推送里）")
        lines = [f"到期待验证决策 {len(rows)} 条（到期日在心，回顾用 verdict=right/wrong/partial）："]
        for d in rows:
            lines.append(f"- #{d['id']} {d['review_date']} 到期｜{d['title'][:44]}"
                         f"｜当时决策：{d['decision_brief'][:60]}")
        lines.append("回顾：hub_decision_review action=review review_id=N verdict=right|wrong|partial")
        return "\n".join(lines)

    if name == "hub_consolidate":
        action = str(arguments.get("action", "proposals")).strip()
        if action == "apply":
            raw = arguments.get("ids", [])
            if isinstance(raw, str):
                raw = [x for x in raw.replace("，", ",").split(",") if x.strip()]
            err, mid = brain.consolidate_apply(root, raw,
                                               str(arguments.get("merged_content", "")),
                                               str(arguments.get("agent", "")))
            if err:
                return f"错误：{err}"
            return (f"已整合为新记忆 #{mid}（validated，证据链挂原条目）——"
                    "原条目退出推送但可追溯；hub_search 仍能按 id 翻到旧条")
        props = brain.consolidate_proposals(root)
        if not props:
            return "（无整合提案——没有疑似重复/矛盾的记忆组）"
        lines = [f"整合提案 {len(props)} 组（只读，执行需确认）："]
        for p in props:
            lines.append(f"- ids={p['ids']} 相似度 {p['sim']}｜{p['verdict']}")
            for c in p["contents"]:
                lines.append(f"    · {c}")
        lines.append("执行：hub_consolidate action=apply ids=[…] merged_content=整合后完整内容")
        return "\n".join(lines)

    if name == "hub_view":
        action = str(arguments.get("action", "list")).strip()
        agent = str(arguments.get("agent", "")).strip()
        if action == "follow":
            return brain.view_follow(root, agent,
                                     str(arguments.get("scope_type", "project")),
                                     str(arguments.get("scope_value", "")))
        if action == "unfollow":
            return brain.view_unfollow(root, agent,
                                       str(arguments.get("scope_type", "project")),
                                       str(arguments.get("scope_value", "")))
        rows = brain.view_list(root, agent)
        if not rows:
            return ("（关注清单空。hub_view action=follow scope_type=project scope_value=项目名"
                    "——关注项目有进展会出现在你的心跳「[关注项目动态]」）")
        lines = [f"关注清单 {len(rows)} 项："]
        for v in rows:
            lines.append(f"- [{v['scope_type']}] {v['scope_value']}（{v['created'][:10]} 关注）")
        return "\n".join(lines)

    if name == "hub_skill_stats":
        st = brain.skill_stats(root)
        lines = [f"技能资产统计：共 {st['total']} 条索引"
                 f"（分布：{' / '.join(f'{k} {v}' for k, v in st['by_agent'].items()) or '无'}）"]
        if st["top_used"]:
            lines.append("真被用的（top）：")
            for s in st["top_used"]:
                lines.append(f"- {s['name']} [{s['agent']}] 命中 {s['hits']} 次"
                             f"（最近 {s['last_hit'][:10] or '从未'}）")
        if st["zero_used"]:
            lines.append(f"零使用 {len(st['zero_used'])} 条（清理或补说明候选，未动作）：")
            for s in st["zero_used"][:15]:
                lines.append(f"- {s['name']} [{s['agent']}] {s['path']}")
        return "\n".join(lines)

    if name == "hub_report":
        import report
        path = report.generate_report(root)
        frs = len(report._frictions(report._collect(root)))
        return (f"反馈报告已生成（单文件 HTML，双击即开）：{path}\n"
                f"检出摩擦点 {frs} 条（每条带修复）；报告里的规则/技能修改均为提案，采纳由人拍板")

    if name == "hub_ops_list":
        lines = ["大脑内置检查工具箱（把重复验证固化成一条命令，输出按省 token 设计）："]
        for n, (desc, need_t) in ops.CHECKS.items():
            lines.append(f"  {n}：{desc}" + ("（需要 target=目录）" if need_t else "（无需 target）"))
        lines.append("执行：hub_ops_run(name, target)——py_compile/git_status/regression/deploy_diff 必填 target=目录路径")
        return "\n".join(lines)

    if name == "hub_ops_run":
        cname = str(arguments.get("name", "")).strip()
        tgt = str(arguments.get("target", "")).strip()
        r = ops.run_check(cname, tgt, root)
        lines = [("✓" if r.get("ok") else "✗") + f" {cname}：{r.get('summary', '')}"]
        lines += [f"  {d}" for d in r.get("detail", [])[:8]]
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

    if name == "hub_status":
        s = brain.status_snapshot(root)
        today = s["today"]
        lines = [f"大脑此刻状态（{s['now']}）——历史靠 hub_search，现在看这里："]
        # ① 什么还是活跃的（会话级 + 项目级）
        if s["sessions"]:
            who = "；".join(f"{x['agent']}（{x['project'] or '未登记项目'}）" for x in s["sessions"])
            lines.append(f"① 在线会话：{who}")
        else:
            lines.append("① 在线会话：无（开工先 hub_heartbeat 登记）")
        if s["active_projects"]:
            ps = "、".join(f"{p['name']}（{p['last_date']}，{p['n']}条）" for p in s["active_projects"])
            lines.append(f"   活跃项目（近7天）：{ps}")
        else:
            lines.append("   活跃项目（近7天）：无")
        # ② 什么变了
        lines.append(f"② 今日新增：记录 {s['records_today']} 条 · 记忆 {s['memories_today']} 条；最近操作：")
        for j in s["journal"]:
            day, hm = (j["ts"][:10], j["ts"][11:16]) if len(j["ts"]) >= 16 else (j["ts"], "")
            when = hm if day == today else day
            note = f"：{j['note'][:40]}" if j["note"] else ""
            lines.append(f"   {when} {j['agent']} {j['action']}{note}")
        # ③ 什么还开着（未解决）
        if s["errors_open"]:
            es = "；".join(f"#{e['id']} {e['title'][:40]}" for e in s["errors_open"])
            lines.append(f"③ 未关闭错误 {s['errors_open_total']} 条：{es}")
        else:
            lines.append("③ 未关闭错误：无")
        if s["handoffs"]:
            hs = "；".join(f"#{h['id']} [{h['project']}] 来自 {h['agent']}" for h in s["handoffs"])
            lines.append(f"   待认领交接 {s['handoffs_total']} 条：{hs}（hub_handoff claim）")
        else:
            lines.append("   待认领交接：无")
        # ④ 什么还重要
        if s["pinned"]:
            pm = "；".join(f"#{p['id']}[{brain.KIND_CN.get(p['kind'], p['kind'])}] {p['content'][:36]}" for p in s["pinned"])
            lines.append(f"④ 置顶记忆 {len(s['pinned'])} 条：{pm}")
        else:
            lines.append("④ 置顶记忆：无")
        if s["stalled"]:
            lines.append(f"   停滞项目 {s['stalled_total']} 个：{'、'.join(s['stalled'])}（超90天无动静，确认结局或归档）")
        # ⑤ 下一步该做什么
        if s["todos"]:
            lines.append("⑤ 待办线索（勾销用 hub_todo_done）：")
            for t in s["todos"]:
                lines.append(f"   [{t['date']} {t['agent']}·{t['project']}] {t['todo'][:60]}")
        else:
            lines.append("⑤ 待办线索：无")
        # ⑥ 主人最新随记（开工视野）：主人的第一人称思想层，agent 感知主人的最新关切
        try:
            latest = brain.latest_notes_for_agent(root, k=3)
        except Exception:
            latest = []
        if latest:
            lines.append("⑥ 主人最新随记（hub_note_read 读全文，与手头工作相关时主动回应）：")
            for n in latest:
                mood = f"（{n['mood']}）" if n["mood"] else ""
                lines.append(f"   #{n['id']} {n['ts'][:10]}{mood} {n['content'][:70]}")
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
                     "properties": {"project": {"type": "string"},
                                    "agent": {"type": "string", "enum": ["zcode", "dsh", "claude", "codex", "hermes", "workbuddy", "unknown"],
                                              "description": "你的 agent 名（注册表内取值，会被校验；显示名如 WorkBuddy/DSH 也接受并自动归一）。确实无法归属时传 unknown，不要编造"},
                                    "content": {"type": "string", "description": "做了什么/验证结果/如何回滚"},
                                    "date": {"type": "string", "description": "YYYY-MM-DD，缺省今天"},
                                    "record_type": {"type": "string", "description": "记录类型（可选）：work_log/decision/todo/note/crystal（结晶一般由任务完成自动生成）；缺省自动推断（含决策段→decision，否则 work_log）"},
                                    "context": {"type": "string", "description": "决策-上下文（AgDR，可选）：当时为什么面临这个选择"},
                                    "options": {"type": "string", "description": "决策-权衡过的选项（可选）：评估了哪些方案、各自为何成/否"},
                                    "decision": {"type": "string", "description": "决策-决策（可选）：最终选了什么"},
                                    "tradeoffs": {"type": "string", "description": "决策-接受的取舍（可选）：选它付出的代价"},
                                    "review_date": {"type": "string", "description": "M11 决策回路（可选）：YYYY-MM-DD——到期后此决策进「待验证」队列（心跳推送+回顾工具），适合「当时判断、日后验证」的决策（如选型/方案）；不传不建"},
                                    "waive": {"type": "string", "description": "豁免门禁（可选）：确属误报时填一句话理由，记录照写但留痕可追溯"}},
                     "required": ["project", "agent", "content"]}},
    {"name": "hub_search", "description": "跨项目全文搜索工作记录/记忆/文件名（记录行带 #id，判断不了价值先看片段，精读用 hub_get_record）",
     "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]}},
    {"name": "hub_note", "description": "记入人的随记（顿悟/想法/摘录，主人第一人称思想）——主人说\"记一下：XXX\"时调用；content 必填，tags/link/mood 可选。随记与工作经验（memories）分层但检索互通",
     "inputSchema": {"type": "object",
                     "properties": {"content": {"type": "string", "description": "随记内容（人说的原话或整理后的想法）"},
                                    "tags": {"type": "string", "description": "标签，逗号分隔（如：顿悟,架构）"},
                                    "link": {"type": "string", "description": "相关链接（URL/文件路径，可选）"},
                                    "mood": {"type": "string", "description": "记下时的心情（可选：兴奋/笃定/疑问/纠结/灵感/低落/平静）"}},
                     "required": ["content"]}},
    {"name": "hub_note_read", "description": "按 id 读取一条主人的随记全文（含心情/标签/上下文）。两种用法：①深挖协议——主人说「深挖随记#N」时读它并反问一个最能展开想法的问题；②整理协议——主人说「整理随记#a #b #c」时逐条读，提炼共同主题成一条记忆，用 hub_memory_write 写入并汇报",
     "inputSchema": {"type": "object",
                     "properties": {"note_id": {"type": "string", "description": "随记 id（如 12）"}},
                     "required": ["note_id"]}},
    {"name": "hub_get_rules", "description": "读取团队协作规范（目录命名/记录格式/铁律）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_memory_read", "description": "读取大脑记忆库（结构化：环境事实/用户偏好/踩坑经验/项目进展/随手记）。带 query 可关键词检索，kind 按类型过滤",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "关键词检索，缺省返回置顶+最近概览"},
                                    "kind": {"type": "string", "enum": list(brain.KINDS)},
                                    "limit": {"type": "integer"}}}},
    {"name": "hub_ask", "description": "问大脑（D2）：把检索升级成「问句 → 结构化回答 + 逐条出处 + 明确弃权」。问「X 项目最大的风险是什么 / 为什么选了 Y / 还有哪些待办 / Z 现在什么状态」，返回带 #id 出处的结论；库里没有依据时明说「我不知道」而不拼凑。零 LLM（规则+检索），只读不计唤起",
     "inputSchema": {"type": "object",
                     "properties": {"question": {"type": "string", "description": "自然语言问句，如「AgentHub 最大的风险是什么」"},
                                    "project": {"type": "string", "description": "限定项目名（缺省则自动从问句里匹配已知项目）"}},
                     "required": ["question"]}},
    {"name": "hub_metacognition", "description": "元认知面板（D10）：大脑的自我报告——我记得什么/忘了什么/可能记错了什么。列出覆盖盲区（有记录零记忆的项目）、低置信现行记忆、疑似矛盾对、久未复核决策、元信息占比、治理统计（取代/过期/作废/证据覆盖）。只读",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_memory_write", "description": "写入一条大脑记忆，供所有 agent 检索。kind 建议明确：环境事实用 fact、用户偏好用 preference、踩坑用 lesson、项目进展用 project。批次A支持：supersedes 取代旧记忆（旧条退出推送但可追溯）、valid_until 有效期止(YYYY-MM-DD)、confidence 置信度(0~1)、evidence 证据链(r:记录id,m:记忆id)",
     "inputSchema": {"type": "object",
                     "properties": {"content": {"type": "string"},
                                    "kind": {"type": "string", "enum": list(brain.KINDS), "description": "记忆类型，缺省 note"},
                                    "tags": {"type": "string", "description": "标签，逗号分隔"},
                                    "project": {"type": "string", "description": "相关项目，可空"},
                                    "agent": {"type": "string", "enum": ["zcode", "dsh", "claude", "codex", "hermes", "workbuddy", "unknown"],
                                              "description": "你的 agent 名（注册表内取值，会被校验）"},
                                    "pinned": {"type": "boolean", "description": "置顶（每次读记忆优先展示）"},
                                    "feedback": {"type": "string",
                                                 "description": "对已有记忆反馈（配合 memory_id 使用）：helpful/not_helpful/stale/wrong 调节显著性；archive 归档该记忆（不硬删）"},
                                    "memory_id": {"type": "integer",
                                                  "description": "要反馈的记忆 id（配合 feedback 使用）"},
                                    "mode": {"type": "string", "enum": ["append", "overwrite"], "description": "append 新增（默认）；overwrite 按内容匹配更新"},
                                    "supersedes": {"type": "integer", "description": "取代的目标记忆 id：旧条自动退出推送但仍可追溯（记忆取代链）"},
                                    "valid_until": {"type": "string", "description": "有效期止 YYYY-MM-DD，到期自动退出推送（双时相时效治理）"},
                                    "confidence": {"type": "number", "description": "置信度 0~1，低置信记忆在治理统计中可识别"},
                                    "evidence": {"type": "string", "description": "证据链，形如 r:2038,m:76（引用记录/记忆 id，provenance 溯源）"},
                                    "maturity": {"type": "string", "enum": list(brain.MATURITY_LEVELS),
                                                 "description": "M6 成熟度（信任分层）：draft 草稿（缺省）/ validated 已验证（写入者已确认过的结论，慎标）/ core 核心（留给人工 pinned+高频自动晋升，勿手填）"}},
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
    {"name": "hub_archive_project", "description": "归档已完结项目（状态改 archived + 目录移入 99_Archive，可逆）。仅限用户明确拍板的项目清理",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "项目目录名"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["project"]}},
    {"name": "hub_list_skills", "description": "列出本机各 agent 的技能库（能力对齐：看别的 agent 会什么）",
     "inputSchema": {"type": "object", "properties": {"agent": {"type": "string", "description": "可选，过滤 agent 名"}}}},
    {"name": "hub_skill_match",
     "description": "技能按需召回（技能原生化）：用一句话描述本次任务，大脑从全机技能库（跨 agent）回 top-N 最相关的技能「名字+说明+文件路径」，替代「全量技能描述常驻上下文」，大幅省 token。命中别家（如 ZCode）的技能也能借用",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "任务描述（如：留学选校、抓B站字幕、Windows批处理、简历改写）"},
                                    "topk": {"type": "integer", "description": "返回条数，默认 3；不建议 >5，多了就是新的元信息灌水"},
                                    "agent": {"type": "string", "description": "可选，限定只搜某个 agent 的技能库"}},
                     "required": ["query"]}},
    {"name": "hub_skill_sync",
     "description": "把本机各 agent 的技能元数据导入大脑 skills 索引（UPSERT，不删行）。不传 rows 则自动扫描全机；首次使用或新增技能后跑一次",
     "inputSchema": {"type": "object",
                     "properties": {"rows": {"type": "array", "description": "可选：[{name,agent,path,desc}]；不传则自动扫描"},
                                    "agent": {"type": "string", "description": "可选，只同步某个 agent（如 ZCode）"}}}},
    {"name": "hub_asset_find",
     "description": "软件资产台账检索（L3 数据层）：装任何工具前先查这里，返回名称/分类/许可/官方源，避免重复下载与山寨源。首批 21 款来自 tools.accat.top",
     "inputSchema": {"type": "object",
                     "properties": {"keyword": {"type": "string", "description": "软件名或用途关键词（如：录屏、远程、压缩、远程控制）"},
                                    "limit": {"type": "integer", "description": "返回条数，默认 8"}},
                     "required": ["keyword"]}},
    {"name": "hub_list_mcps", "description": "列出本机各 agent 已配置的 MCP 服务器",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_bootstrap_status",
     "description": "各 agent 的接入状态（只读）：引导规则是否已注入 + MCP 是否已配 agenthub + 送达通道。用于核对「谁还没接上大脑」",
     "inputSchema": {"type": "object", "properties": {"agent": {"type": "string", "description": "可选，只看某个 agent"}}}},
    {"name": "hub_list_agents", "description": "列出注册 agent 身份表（在线状态/累计记录/心跳次数/最近活跃）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_list_todos", "description": "列出工作记录里的待办/承诺线索（后续/待验证/下一步…句式，元认知）",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "条数，默认20"}}}},
    {"name": "hub_duplicates",
     "description": "数据完整性检测（只读）：列出被 agent 前缀拆散的重复项目目录、内容逐字重复的记录组，以及迁移存量口径。不做任何修改",
     "inputSchema": {"type": "object", "properties": {
         "min_len": {"type": "integer", "description": "判定重复记录的最短内容长度，默认 80"}}}},
    {"name": "hub_handoff",
     "description": "交接协议：换 agent 接手前用 action=create 留下 typed 上下文（做完/下一步/已排除的路），接手方 action=list 查看、action=claim 认领（同一交接仅一人可认领成功）",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["list", "create", "claim"],
                                               "description": "list 列交接（缺省）/ create 创建 / claim 认领"},
                                    "project": {"type": "string", "description": "create 必填；list 可按项目过滤"},
                                    "agent": {"type": "string", "description": "你的 agent 名（create/claim 必填）"},
                                    "done": {"type": "string", "description": "create：做完了什么"},
                                    "next_step": {"type": "string", "description": "create：下一步是什么"},
                                    "excluded": {"type": "string", "description": "create：哪些路已排除（防接手方重蹈）"},
                                    "handoff_id": {"type": "integer", "description": "claim：交接 id"},
                                    "include_claimed": {"type": "boolean", "description": "list：含已认领"}}}},
    {"name": "hub_signal_send",
     "description": "M3 发信号：给指定 agent 发消息（to_agent），或留空广播给全员。对方下次心跳自动收到未读提醒；已读有回执。与 hub_handoff 分工：signal=agent 间直接消息，handoff=项目移交（typed 上下文+认领）",
     "inputSchema": {"type": "object",
                     "properties": {"agent": {"type": "string", "description": "你的 agent 名（发件人）"},
                                    "to_agent": {"type": "string", "description": "收件人 agent 名；留空=广播（全员可见）"},
                                    "content": {"type": "string", "description": "消息内容（必填）"},
                                    "kind": {"type": "string", "enum": list(brain.SIGNAL_KINDS),
                                             "description": "msg 普通消息 / ack_request 需回执 / alert 告警"},
                                    "project": {"type": "string", "description": "关联项目，可空"}},
                     "required": ["agent", "content"]}},
    {"name": "hub_signals",
     "description": "M3 信号收件箱：发给我的 + 广播，未读（●）置顶标；unread_only=1 只要未读。与 hub_events 分工：signal=需消费的消息（已读回执），events=只增事件流（订阅式）",
     "inputSchema": {"type": "object",
                     "properties": {"agent": {"type": "string", "description": "你的 agent 名"},
                                    "unread_only": {"type": "boolean", "description": "只要未读"},
                                    "limit": {"type": "integer", "description": "条数，默认 30"}}}},
    {"name": "hub_signal_read",
     "description": "M3 标记信号已读（回执）：幂等，广播按人各记一次；只能标发给自己的（或广播）",
     "inputSchema": {"type": "object",
                     "properties": {"signal_id": {"type": "integer", "description": "信号 id"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["signal_id", "agent"]}},
    {"name": "hub_checkpoint",
     "description": "M4 检查点：长任务执行中存进展快照（断点续跑/换人接手/隔天续跑不用从零翻记录）。认领时自动存开工资锚点；action=list 看时间线、latest 只看最新一条",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["save", "list", "latest"],
                                               "description": "save 存（缺省）/ list 时间线 / latest 最新一条"},
                                    "task_id": {"type": "integer", "description": "任务 id"},
                                    "content": {"type": "string", "description": "save：进展快照（做完了什么/下一步/卡在哪）"},
                                    "kind": {"type": "string", "enum": list(brain.CHECKPOINT_KINDS),
                                             "description": "manual 手写（缺省）/ auto_claim 认领自动"},
                                    "agent": {"type": "string", "description": "你的 agent 名（save 必填）"}},
                     "required": ["task_id"]}},
    {"name": "hub_sentry",
     "description": "M5 哨兵：跑大脑自身健康巡检（闸门/规模/备份/重复记忆/inbox/停滞），把需关注项聚合成广播 alert——所有 agent 下次开工即在心跳里可见，不用各自跑体检。同内容 24h 冷却防刷屏",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_quarantine",
     "description": "M8 隔离区：把可疑的记录/记忆移出检索与推送（status→quarantine，不删除、可解除）。与软删的分工：deleted=撤销不可见，quarantine=观察区可批量处理",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["list", "quarantine", "release"],
                                               "description": "list 隔离区清单（缺省）/ quarantine 隔离 / release 解除"},
                                    "target": {"type": "string", "enum": ["record", "memory"],
                                               "description": "操作对象类型，缺省 record"},
                                    "id": {"type": "integer", "description": "目标 id"},
                                    "reason": {"type": "string", "description": "隔离原因（quarantine 用）"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}}}},
    {"name": "hub_decision_review",
     "description": "M11 决策回路：写决策记录时带 review_date，到期进「待验证」队列（心跳开工推送也会提醒），回顾时记 verdict（right 对/wrong 错/partial 部分对）——决策写了没人回顾=白写",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["list", "review", "history"],
                                               "description": "list 到期待验证（缺省）/ review 记回顾结论 / history 回顾史"},
                                    "review_id": {"type": "integer", "description": "review：回顾项 id（list 里给出）"},
                                    "verdict": {"type": "string", "enum": list(brain.REVIEW_VERDICTS),
                                                "description": "review：right 当时决策对 / wrong 错 / partial 部分对"},
                                    "note": {"type": "string", "description": "review：结论说明"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}}}},
    {"name": "hub_consolidate",
     "description": "M7 记忆整合：action=proposals 看疑似重复/矛盾记忆组的整合提案（只读）；action=apply 执行——写一条整合记忆（validated），原条目 superseded_by 退出推送但可追溯。整合是高危动作，执行需显式确认",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["proposals", "apply"],
                                               "description": "proposals 提案（缺省）/ apply 执行整合"},
                                    "ids": {"type": "array", "items": {"type": "integer"},
                                            "description": "apply：要整合的记忆 id 列表（至少 2 条，取 proposals 给出的）"},
                                    "merged_content": {"type": "string",
                                                       "description": "apply：整合后的完整内容（不是摘要，要能独立读懂）"},
                                    "agent": {"type": "string", "description": "你的 agent 名（apply 必填）"}}}},
    {"name": "hub_view",
     "description": "M9 视角：关注项目/标签（follow/unfollow/list）。关注的项目近 7 天有进展，会出现在你的心跳「[关注项目动态]」——多 agent 场景各看各的地盘，不开工也知道进展",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["list", "follow", "unfollow"],
                                               "description": "list 我的关注（缺省）/ follow 关注 / unfollow 取消"},
                                    "scope_type": {"type": "string", "enum": ["project", "tag"],
                                                   "description": "关注类型，缺省 project"},
                                    "scope_value": {"type": "string", "description": "项目名或标签"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}}}},
    {"name": "hub_skill_stats",
     "description": "M12 技能资产统计：哪些技能真被用（命中次数）、哪些零使用（清理或补说明候选）——技能索引进化了，用不用数据说话",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_report",
     "description": "生成反馈报告（单文件 HTML 双击即开）：mentor 式九章节——你在做什么/时间分布/做对了什么/摩擦点（每条带修复）/规则修改提案。提案需人确认，系统不自动改",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_ops_list", "description": "大脑内置检查工具箱清单：把 agent 每次重复做的验证（编译检查/跑回归/部署一致性/git/环境预检）固化成可一键执行的检查项，输出精简省 token",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_ops_run", "description": "执行一项内置检查并返回精简摘要（不返回全量日志）。py_compile/git_status/regression/deploy_diff 必填 target=目录路径；env/brain 无需。regression 会执行 target 下的测试脚本（约1分钟），其余只读",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string", "description": "检查项名（先 hub_ops_list 看清单）",
                                             "enum": sorted(ops.CHECKS)},
                                    "target": {"type": "string", "description": "目标目录绝对路径（按检查项要求）"}},
                     "required": ["name"]}},
    {"name": "hub_health", "description": "大脑体检报告：规模/结晶率/待办线索/疑似重复记忆/检索活跃度+建议",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_user_profile", "description": "用户画像：活跃时段/记录分工/技术栈词频/用户纠正史 top——判断用户偏好与习惯时先查（零 LLM 实测统计）",
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
    {"name": "hub_get_record", "description": "读取单条工作记录（渐进披露三级：hub_search 行 → abstract/outline → full，每级成本差一个量级）",
     "inputSchema": {"type": "object",
                     "properties": {"record_id": {"type": "integer", "description": "记录 id，如 2037"},
                                    "level": {"type": "string", "enum": ["full", "abstract", "outline"],
                                              "description": "full=全文（缺省）；abstract=前200字；outline=段头骨架"}},
                     "required": ["record_id"]}},
    {"name": "hub_get_progress", "description": "获取所有 agent 最近的工作时间线（进度对齐）",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "条数，默认30"}}}},
    {"name": "hub_status", "description": "大脑此刻状态（连续性层）：谁在线/活跃项目/今日变更/未关闭错误与待认领交接/置顶记忆与停滞项目/待办线索——历史靠 hub_search，现在看这里",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_help",
     "description": "起步指北（任何 profile 常驻可见）：当前 profile 与可见工具清单 + 发现/调用/预演/截断四件事的用法",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_search_tools",
     "description": "按关键词搜索全部工具（含当前 profile 藏起的；藏起≠禁用）——找不到某个能力时先来这里",
     "inputSchema": {"type": "object",
                     "properties": {"keyword": {"type": "string",
                                                "description": "关键词（匹配工具名/说明子串），缺省列出全部"}}}},
    {"name": "hub_call_tool",
     "description": "调用任意工具（含被 profile 藏起的）——name + arguments 透传；写工具同样受 dry_run/confirm/审计约束",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string", "description": "目标工具名"},
                                    "arguments": {"type": "object", "description": "目标工具的参数对象"}},
                     "required": ["name"]}},
    {"name": "hub_audit",
     "description": "写操作审计（P3-1）：最近的写工具调用记录（谁/何时/写了什么/undo 句柄）",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer", "description": "条数，默认 20"}}}},
    {"name": "hub_flush_index",
     "description": "语义索引维护：把缺失向量补进 embeddings（force=true 全量重编码）。语义依赖缺失时如实报错（fail-open）",
     "inputSchema": {"type": "object",
                     "properties": {"force": {"type": "boolean", "description": "true=全量重编码（默认只补缺失）"}}}},
    {"name": "hub_task_add",
     "description": "新建任务（P3-2 状态机 + M1 工作图）：title 必填；blocked_by 非空则初始为阻塞态；priority/deps/due 见字段说明。开工前认领（hub_claim），选活先看 hub_next",
     "inputSchema": {"type": "object",
                     "properties": {"title": {"type": "string", "description": "任务标题（一句话）"},
                                    "project": {"type": "string", "description": "所属项目，可空"},
                                    "detail": {"type": "string", "description": "细节/验收标准，可空"},
                                    "blocked_by": {"type": "string", "description": "被谁/什么阻塞（非空则初始 status=blocked）"},
                                    "priority": {"type": "integer", "description": "优先级 0=急(P0)/1=高/2=中(默认)/3=低"},
                                    "deps": {"type": "string", "description": "前置任务 id，逗号分隔（如 '12,15'）；未全部完成前不被 hub_next 推荐、不可认领"},
                                    "due": {"type": "string", "description": "截止日（如 2026-10-20），可空"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["title"]}},
    {"name": "hub_claim",
     "description": "认领任务（P3-2）：pending → in_progress(owner)。同一任务同时只有一家能认领成功；超 2h 未更新自动可被接管（防死锁）",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "任务 id"},
                                    "agent": {"type": "string", "description": "你的 agent 名（必填）"}},
                     "required": ["task_id", "agent"]}},
    {"name": "hub_task_update",
     "description": "任务状态流转：pending(释放)/in_progress/blocked(写 blocked_by)/done。done 会自动结晶成 crystal 记录（M2）。in_progress 任务只有持有者可流转（空 agent=管理面）。deps/due 传 '-' 可清除",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "任务 id"},
                                    "status": {"type": "string", "enum": list(brain.TASK_STATUSES)},
                                    "blocked_by": {"type": "string", "description": "阻塞原因（status=blocked 时用）"},
                                    "detail": {"type": "string", "description": "补充细节/进展，可空"},
                                    "priority": {"type": "integer", "description": "优先级 0~3（可改）"},
                                    "deps": {"type": "string", "description": "前置任务 id（逗号分隔；'-' 清除）"},
                                    "due": {"type": "string", "description": "截止日（'-' 清除）"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["task_id"]}},
    {"name": "hub_tasks",
     "description": "任务列表（P3-2）：进行中→阻塞→待办→完成 排序；可按 status/owner/project 过滤。看谁在干什么、什么被卡住",
     "inputSchema": {"type": "object",
                     "properties": {"status": {"type": "string", "enum": list(brain.TASK_STATUSES)},
                                    "owner": {"type": "string", "description": "按认领人过滤"},
                                    "project": {"type": "string", "description": "按项目过滤"},
                                    "limit": {"type": "integer", "description": "条数，默认 50"}}}},
    {"name": "hub_next",
     "description": "下一步推荐（M1 前沿队列）：依赖已满足、可直接认领的任务，按优先级排序；附进行中/可接管/等待依赖三段。开工前先问它「我该干什么」",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "只看某项目，可空"},
                                    "agent": {"type": "string", "description": "你的 agent 名（标出你手上的活与可接管项）"},
                                    "limit": {"type": "integer", "description": "推荐条数，默认 5"}}}},
    {"name": "hub_crystallize",
     "description": "任务结晶（M2）：把已完成任务的完成链（任务 + 完成期间同项目记录）聚合为一条 crystal 记录便于回顾；幂等（已有则复用，force=true 软删旧结晶重做）。任务 done 时已自动结晶，本工具用于补做/重做",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "integer", "description": "任务 id"},
                                    "force": {"type": "boolean", "description": "true=软删旧结晶后重做"}},
                     "required": ["task_id"]}},
    {"name": "hub_event_write",
     "description": "写一条事件（P3-2 事件流）：写进展/求助/交接信号，其他 agent 可订阅（hub_events）",
     "inputSchema": {"type": "object",
                     "properties": {"content": {"type": "string", "description": "事件内容（一句话进展）"},
                                    "kind": {"type": "string", "enum": list(brain.EVENT_KINDS),
                                             "description": "事件类型，缺省 progress"},
                                    "project": {"type": "string", "description": "相关项目，可空"},
                                    "ref": {"type": "string", "description": "关联引用（记录/任务 id 等），可空"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["content"]}},
    {"name": "hub_events",
     "description": "读事件流（倒序）；增量订阅传 since_id=上次最大 id 只取新事件（agent 之间的轻量协作通道）",
     "inputSchema": {"type": "object",
                     "properties": {"since_id": {"type": "integer", "description": "只要 id 大于它的新事件"},
                                    "limit": {"type": "integer", "description": "条数，默认 30"},
                                    "project": {"type": "string", "description": "按项目过滤"},
                                    "agent": {"type": "string", "description": "按来源 agent 过滤"}}}},
]


# ---------------------------------------------------------------- P3-1 profile 切片 / 写工具清单

# 任何 profile 都可见的最小面（hub_help 是发现入口）
PROFILE_ALWAYS = ("hub_help", "hub_status", "hub_heartbeat")
# reader：最小只读切片（约 3~8 个：总览/检索/精读/规范/发现）
PROFILE_READER = PROFILE_ALWAYS + ("hub_get_rules", "hub_search", "hub_get_record",
                                   "hub_search_tools")
# worker：日常干活（记录/记忆/待办/认领/事件/审计），管理类工具留给 admin
PROFILE_WORKER = PROFILE_READER + (
    "hub_call_tool", "hub_get_progress", "hub_get_project", "hub_list_projects",
    "hub_memory_read", "hub_memory_write", "hub_log_work", "hub_note", "hub_note_read",
    "hub_todo_done", "hub_list_todos", "hub_report_error", "hub_distill", "hub_undo",
    "hub_env_list", "hub_env_set", "hub_handoff", "hub_audit", "hub_flush_index",
    "hub_task_add", "hub_task_update", "hub_claim", "hub_tasks", "hub_next", "hub_crystallize",
    "hub_event_write", "hub_events",
    "hub_signal_send", "hub_signals", "hub_signal_read",  # M3 信号：协作总线要给干活的 agent
    "hub_checkpoint",  # M4 检查点：长任务断点续跑
    "hub_sentry",  # M5 哨兵：巡检广播
    "hub_quarantine",  # M8 隔离区：治理工具要给干活的 agent
    "hub_decision_review",  # M11 决策回路：到期验证
    "hub_consolidate", "hub_view", "hub_skill_stats",  # M7/M9/M12：治理三件套
    "hub_skill_match",  # 技能原生化：按需召回要给所有干活的 agent（含 worker）
    "hub_asset_find",  # 技能原生化：装工具前查台账，同属干活常用
)
PROFILES = {"reader": PROFILE_READER, "worker": PROFILE_WORKER, "admin": None}  # None=全量

# 技能原生化·阶段4：心跳附带技能建议的进程内去重键（agent, project）-> 上次的 note。
# 任务没换就不重复推——主人明确反感元信息占位，同一任务灌两次等于噪音。
_LAST_SKILL_NOTE: dict = {}

# 写工具清单（P3-1）：dry_run 预演、confirm 闸门、写后审计三者都按它判定
WRITE_TOOLS = {
    "hub_log_work", "hub_create_project", "hub_note", "hub_memory_write", "hub_heartbeat",
    "hub_report_error", "hub_undo", "hub_archive_project", "hub_todo_done", "hub_env_set",
    "hub_ops_run", "hub_handoff", "hub_report", "hub_flush_index",
    "hub_task_add", "hub_task_update", "hub_claim", "hub_crystallize", "hub_event_write",
    "hub_skill_sync",  # 技能原生化：写索引（走 dry_run/confirm 闸门）
    "hub_signal_send", "hub_signal_read",  # M3 信号：发消息/标已读
    "hub_checkpoint",  # M4 检查点：写进展快照
    "hub_sentry",  # M5 哨兵：跑巡检并发广播
    "hub_quarantine",  # M8 隔离区：可疑内容移出检索（不删）
    "hub_decision_review",  # M11 决策回路：到期验证决策对错
    "hub_consolidate",  # M7 整合：重复记忆组合并（apply 走闸门）
    "hub_view",  # M9 视角：关注项目/标签
    "hub_skill_stats",  # M12 技能资产统计
}


def visible_tools() -> list:
    """当前 profile 下 tools/list 的可见切片（admin=全量；藏起≠禁用，仍可直接调用）。"""
    allow = PROFILES.get(SERVER_PROFILE)
    if allow is None:
        return list(TOOLS)
    allow = set(allow)
    return [t for t in TOOLS if t["name"] in allow]


# 统一字段注入：所有工具支持 max_chars；写工具额外支持 dry_run / confirm（契约单一来源）
for _t in TOOLS:
    _props = _t.setdefault("inputSchema", {}).setdefault("properties", {})
    _props.setdefault("max_chars", {
        "type": "integer",
        "description": f"结果字符预算（默认 {DEFAULT_MAX_CHARS}；0=不截断）。超预算截断并在文末标注，"
                       "完整内容在 structuredContent"})
    if _t["name"] in WRITE_TOOLS:
        _props.setdefault("dry_run", {"type": "boolean",
                                      "description": "true=只预演不执行（不落库）"})
        _props.setdefault("confirm", {"type": "boolean",
                                      "description": "写操作显式确认；传 false 时返回 input_required 且不执行"
                                                     "（return-and-resume，服务端不阻塞不回调）"})


# ---------------------------------------------------------------- MCP 协议层

def _shape_result(text: str, arguments: dict) -> tuple:
    """结果契约（P3-1）：默认预算内原样返回；超预算 → 截断 + 文末明确标注 + structuredContent。
    max_chars=0 关闭截断；非法值回落默认（撑不住时自我降级并明确告知，抄 aider 硬预算）。"""
    try:
        budget = (int(arguments.get("max_chars"))
                  if arguments.get("max_chars") is not None else DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        budget = DEFAULT_MAX_CHARS
    if budget <= 0 or len(text) <= budget:
        return text, None
    meta = {"total": len(text), "shown": budget, "truncated": True, "cursor": budget}
    foot = (f"\n…（已截断：原文 {len(text)} 字符，此处显示前 {budget}；完整内容见 "
            "structuredContent.full_text，或传 max_chars=0 关闭截断）")
    return text[:budget] + foot, {"meta": meta, "full_text": text}


def _audit_write(root: str, tname: str, arguments: dict, text: str) -> None:
    """写操作审计（P3-1）：落一行 op_audit，附 undo 句柄（识别出记录/记忆 id 时带上）。"""
    try:
        summary = (text or "").strip().splitlines()[0] if text else ""
        kind, ref = "", ""
        m = re.search(r"records#(\d+)", text or "")
        if m:
            kind, ref = "record", m.group(1)
        else:
            m = re.search(r"记忆 ?#(\d+)", text or "")
            if m:
                kind, ref = "memory", m.group(1)
        brain.audit_add(root, str(arguments.get("agent") or ""), tname,
                        str(arguments.get("project") or ""), summary, kind, ref)
    except Exception:  # noqa: BLE001 审计失败不影响主流程
        pass


def handle_message(msg: dict, root: str) -> dict | None:
    """处理一条 JSON-RPC 请求，返回响应；通知返回 None。"""
    method = msg.get("method", "")
    mid = msg.get("id")
    is_notification = mid is None
    params = msg.get("params") or {}

    if method == "initialize":
        req_ver = params.get("protocolVersion", PROTOCOL_VERSION)
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"protocolVersion": req_ver, "capabilities": {"tools": {}},
                           "serverInfo": SERVER_INFO}}
    if method == "server/discover":
        # P3-1 版本协商：未知版本 → -32022；已知/未传 → 返回能力面（dual-era：initialize 握手保留）
        pv = params.get("protocolVersion")
        if pv and pv not in SUPPORTED_PROTOCOL_VERSIONS:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32022, "message": "UnsupportedProtocolVersionError",
                              "data": {"supported": list(SUPPORTED_PROTOCOL_VERSIONS)}}}
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"supportedProtocolVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                           "serverInfo": SERVER_INFO, "agentId": SERVER_AGENT,
                           "profile": SERVER_PROFILE,
                           "visibleToolCount": len(visible_tools()),
                           "totalToolCount": len(TOOLS)}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": visible_tools()}}
    if method == "tools/call":
        tname = params.get("name", "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        # P3-1 人工确认闸门：写操作显式 confirm=false → return-and-resume（不执行、不阻塞、不回调）
        if tname in WRITE_TOOLS and args.get("confirm") is False:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"resultType": "input_required", "isError": False,
                               "content": [{"type": "text", "text": (
                                   f"调用未执行：{tname} 是写操作。人工确认后重发并传 confirm=true。"
                                   "（return-and-resume：服务端不阻塞等待，也不会主动回调。）")}]}}
        # P3-1 dry_run：写操作预演（不落库、不审计）
        if tname in WRITE_TOOLS and args.get("dry_run") is True:
            preview = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items()
                                if k not in ("dry_run", "confirm", "max_chars"))
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": (
                        f"dry_run（未执行）：{tname} 预演通过，将写入 → {preview or '(无参数)'}\n"
                        "未落任何库；去掉 dry_run 后正式执行。")}], "isError": False}}
        try:
            text = call_tool(tname, args, root)
            if tname in WRITE_TOOLS:
                _audit_write(root, tname, args, text)
            text, structured = _shape_result(text, args)
            result = {"content": [{"type": "text", "text": text}], "isError": False}
            if structured is not None:
                result["structuredContent"] = structured
            return {"jsonrpc": "2.0", "id": mid, "result": result}
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


def serve(root: str, fixed: bool = False, agent: str = "", profile: str = "") -> int:
    """stdio 主循环。EOF/键盘中断退出。

    fixed=False 时每条请求重读 config 的 root：根目录迁移后旧进程自动跟随，
    杜绝"进程启动时缓存旧路径、静默读写幽灵旧库"（2026-09-30 搬家实测踩坑）。

    agent（B1）：接入配置用 `--agent <名>` 传入的进程身份，**启动时锁定**，
    本进程存活期内不变（stdio 一进程一客户端，身份无歧义）。
    留空=旧接入条目，退回调用参数自报语义（兼容，但会失去防填错能力）。
    """
    global SERVER_AGENT, SERVER_PROFILE
    log = lambda s: print(f"[agenthub-mcp] {s}", file=sys.stderr)  # noqa: E731
    if profile:
        if profile in PROFILES:
            SERVER_PROFILE = profile
            log(f"profile 已设置：{profile}（tools/list 可见 {len(visible_tools())}/{len(TOOLS)}；"
                "藏起≠禁用，hub_search_tools/hub_call_tool 可及）")
        else:
            log(f"⚠ --profile「{profile}」未知（可用 {sorted(PROFILES)}），保持 admin 全量")
    if agent:
        # 归一 + 注册表校验：配错名字要当场说清楚，而不是静默按错的写
        key = core.agent_key(agent)
        reg = core.agent_known_names(root)
        if key in reg:
            SERVER_AGENT = key
            log(f"进程身份已锁定：{SERVER_AGENT}（来自 --agent {agent}）")
        else:
            SERVER_AGENT = key or ""
            log(f"⚠ --agent「{agent}」不在注册表 {sorted(reg)}，仍按 {SERVER_AGENT!r} 锁定；"
                f"请核对接入配置")
    else:
        log("未指定 --agent：身份退回工具调用参数自报（建议在接入配置里补 --agent <名>）")
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


def parse_argv(argv: list) -> tuple[str, bool, str, str]:
    """解析命令行：返回 (root, fixed, agent, profile)。

    支持（顺序无关）：
        agenthub_mcp.py                        → 动态跟随 config
        agenthub_mcp.py --agent zcode          → 动态跟随 + 锁定身份（**接入配置的标准形态**）
        agenthub_mcp.py --profile reader       → P3-1 profile 切片（reader/worker/admin，缺省 admin=全量）
        agenthub_mcp.py <root>                 → 固定根目录（向后兼容）
        agenthub_mcp.py --agent zcode --profile worker <root> → 三者兼有

    ★ `--agent` 必须用**具名参数**，不能复用位置参数：位置参数语义是 root，
      且一旦传入就切"固定模式"（根目录搬家后旧接入会指向失效路径，2026-10-01 踩过）。
      故只传 --agent 时仍保持动态跟随，两者互不干扰。--profile 同为具名参数。
    """
    agent = ""
    profile = ""
    rest: list = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--agent":
            if i + 1 < len(argv):
                agent = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if a.startswith("--agent="):
            agent = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--profile":
            if i + 1 < len(argv):
                profile = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if a.startswith("--profile="):
            profile = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    fixed = len(rest) > 0
    root = rest[0] if fixed else core.get_root()
    return root, fixed, agent, profile


def main():
    root, fixed, agent, profile = parse_argv(sys.argv[1:])
    sys.exit(serve(root, fixed, agent, profile))


if __name__ == "__main__":
    main()
