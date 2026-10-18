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
import time
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
SERVER_INFO = {"name": "agenthub", "version": "2.40.40"}  # v2.40.29–.39=上一轮十轮（备份/守卫下沉/性能/闸门/可观测/schema 版本/哨兵）；本版=R1（第二轮）·C1 补 MCP 工具 annotations：69 个工具此前**全部无 annotations**，host 会退回「每次都问用户」的保守默认；现按既有集合单一来源派生注入 readOnlyHint(35)/destructiveHint(9,=HIGH_BLAST)/idempotentHint(10)——确认 UX 交给 host 按规范决定，不再靠服务端改默认（上一轮 B3 走了弯路）
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05",)  # P3-1 协商集合（dual-era：initialize 握手保留，任意版本回显）
SERVER_PROFILE = "admin"     # P3-1 profile 切片：admin=全量 / worker=日常读写 / reader=最小只读
DEFAULT_MAX_CHARS = 4000     # hub_* 结果默认字符预算；0=不截断；超出截断并在文末标注 + structuredContent
# 响应体积硬上限（防 stdio 管道缓冲死锁 + 防 agent 上下文被巨量结果冲爆）：
# ① STRUCTURED_FULL_CAP —— structuredContent.full_text 内联上限（默认路径也会把完整原文塞进
#    full_text，不封顶则单条大结果就能让 JSON 响应无限膨胀；16K 足够程序化取用又远低于管道缓冲）；
# ② HARD_MAX_CHARS —— max_chars<=0（显式关闭截断）时的兜底上限，只拦病理性/失控输出（>64K），
#    正常工具（<64K）的「不截断」契约不变；>64K 必为异常，截断并明确告知，避免卡死客户端。
STRUCTURED_FULL_CAP = 16 * 1024
HARD_MAX_CHARS = 64 * 1024
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
        return f"错误：AgentHub 根目录不存在：{root}（检查 ~/.agenthub/config.json 的 root 指向，或启动时用 --root 指定数据根目录）"
    # 集中式必填校验（MCP 官方 / mcpcat.io：执行前按声明 schema 校验入参，防把失败当成功）。
    # 无害性论证（2026-10-13）：仅判「键缺失」不判空值（空值由下游工具自身「错误：」文案处理）；
    # 豁免 agent（进程锁定身份 apply_server_agent 已兜底，强制要求会误杀 6 个已接入 agent 的正常调用）；
    # 豁免 hub_checkpoint（task_id 仅 save 必填，list/latest 不需要，且其 save 路径内部已校验）。
    miss = _required_missing(name, arguments)
    if miss:
        need = "、".join(_effective_required(name))
        return f"错误：必填参数 {'、'.join(miss)} 缺失（{name} 需要：{need}）{_example_hint(name)}"
    # 集中式整数入参校验（Round 13）：声明为整数的字段坏类型给清晰错误，
    # 不再由各工具 try/except 静默回落默认值（静默回落=模型以为参数生效了）。
    bad_int = _bad_int_param(name, arguments)
    if bad_int:
        return bad_int
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
            return f"错误：未知工具 {inner}（用 hub_search_tools 搜索可用工具，如 hub_search_tools(keyword=\"错误\")）"
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
        ok, due_s = _norm_date(arguments.get("due"), "due")
        if not ok:
            return due_s
        err, tid = brain.task_add(root, str(arguments.get("title", "")),
                                  project=str(arguments.get("project", "")),
                                  detail=str(arguments.get("detail", "")),
                                  blocked_by=str(arguments.get("blocked_by", "")),
                                  agent=str(arguments.get("agent", "")),
                                  priority=arguments.get("priority", brain.DEFAULT_PRIORITY),
                                  deps=str(arguments.get("deps", "")),
                                  due=due_s)
        if err:
            return "错误：" + err
        return (f"任务已建 #{tid}：{str(arguments.get('title', ''))[:60]}\n"
                "认领：hub_claim；下一步推荐：hub_next；列表：hub_tasks")

    if name == "hub_claim":
        err, row = brain.task_claim(root, arguments.get("task_id"),
                                    str(arguments.get("agent", "")))
        if err:
            return "错误：" + err
        return (f"认领成功：任务 #{row['id']} [{brain.TASK_CN.get(row['status'], row['status'])}] "
                f"{row['title'][:60]} —— owner={row['owner']}"
                f"（超 {brain.CLAIM_TTL_HOURS}h 未更新自动可被接管）")

    if name == "hub_task_update":
        ok, due_s = _norm_date(arguments.get("due"), "due")
        if not ok:
            return due_s
        err, row = brain.task_update(root, arguments.get("task_id"),
                                     status=str(arguments.get("status", "")),
                                     agent=str(arguments.get("agent", "")),
                                     blocked_by=str(arguments.get("blocked_by", "")),
                                     detail=str(arguments.get("detail", "")),
                                     priority=arguments.get("priority"),
                                     deps=str(arguments.get("deps", "")),
                                     due=due_s)
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
            return (f"错误：项目不存在：{pname}。相近项目：{'、'.join(close) if close else '无'}。"
                    "项目目录名须与磁盘目录名一致（「对象-问题」结构），先用 hub_list_projects 核对准确名；"
                    "例：hub_get_project(project=\"AgentHub-多agent文件统一管理\")")
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
            return (f"错误：record_type 只能是 {'/'.join(brain.RECORD_TYPES)}（收到 {rt!r}）。"
                    "例：record_type=\"work_log\"（含决策段时传 decision，或省略自动推断）")
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
        ok, rdate = _norm_date(arguments.get("review_date"), "review_date")
        if not ok:
            return rdate
        rec_id = brain.add_record(root, pname, agent, date, _auto_title(content, date, agent), content,
                                  context=ctx, options=opts, decision=dec, tradeoffs=tro,
                                  record_type=rt,
                                  review_date=rdate)
        core.journal(root, agent, "log_work", f"brain:records#{rec_id}", note=f"{date}（{agent}）")
        herr, active = brain.heartbeat_touch(root, agent, pname)
        warn = _conflict_warn(active)
        out = f"已记入大脑（records#{rec_id}，项目 {pname}）{warn}{_template_nudge(content, rt)}"
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
        # Y2：写了记录即评估 record 触发器（keyword 走内容包含匹配）
        try:
            fired = brain.trigger_eval(root, "record",
                                       {"agent": agent, "project": pname, "content": content,
                                        "keyword": content, "record_id": rec_id}, budget_sec=20)
        except Exception:  # noqa: BLE001  自动化绝不阻断正常记录
            fired = []
        for f in fired:
            if f.get("status") == "cooled":
                continue
            mark = "√" if f["status"] == "fired" else "×"
            out += f"\n[自动化] {mark} #{f['id']} {f['name']}：{(f.get('text') or '')[:90]}"
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
        if not kw:
            # 空关键词不该走检索再回报"无结果"——那是把参数问题伪装成数据问题，
            # agent 收到"无结果"会去换关键词重试，永远修不好（2026-10-13 体验审计）。
            return ("错误：keyword 必填且不能为空（如 hub_search keyword=备份）。"
                    "想浏览全部内容用 hub_list_projects / hub_get_progress")
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
        if any(r.get("_weak") for r in res["records"]) or any(m.get("_weak") for m in res["memories"]):
            lines.insert(1, "（未找到多关键词同时命中的结果，以下为部分命中的弱相关——可拆成更短的词重查）")
        for r in res["records"]:
            src = (f"（图关联自{r['_cross'][2:]}）" if r.get("_cross")
                   else "（图关联）" if "graph" in (r.get("_channels") or {}) else "")
            lines.append(f"[记录#{r['id']}] {r['date']} {r['project']}（{r['agent']}）：{r['title'][:60]}{src}")
            snip = _snippet(r["content"], words)
            if snip:
                lines.append(f"  ↳ {snip}")
        for mem in res["memories"]:
            src = (f"（图关联自{mem['_cross'][2:]}）" if mem.get("_cross")
                   else "（图关联）" if "graph" in (mem.get("_channels") or {}) else "")
            lines.append(f"[记忆#{mem['id']}] {brain.KIND_CN.get(mem['kind'], mem['kind'])}：{mem['content'][:100]}{src}")
        # 矛盾提示（2026-10-13）：业界把冲突检测定位为「取回后、进上下文前」的标准步骤——
        # 不检测就交给 LLM 自己发现，行为不确定且不可审计。这里只提示，不替用户裁决。
        conflicted = [m for m in res["memories"] if m.get("_conflict_with")]
        if conflicted:
            pairs = set()
            for m in conflicted:
                for other in m["_conflict_with"]:
                    pairs.add(tuple(sorted((m["id"], other))))
            lines.append(f"⚠️ 检出 {len(pairs)} 对疑似互斥记忆（一条像在修正另一条）："
                         + "、".join(f"#{a}<->#{b}" for a, b in sorted(pairs)[:3])
                         + "。裁决：用 hub_memory_write supersedes=旧id 取代，或 hub_consolidate 看整合提案")
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
            return "错误：content 必填——人说的原话/随记内容。例：hub_note(content=\"主人说：记一下…\")"
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
        mb = mc.get("meta_breakdown") or {}
        if mb.get("by_kind"):
            kinds = "、".join(
                f"{k} {v['meta'] * 100 // max(1, v['total'])}%"
                for k, v in sorted(mb["by_kind"].items(), key=lambda x: -x[1]["total"]))
            lines.append(f"   元信息拆解（{kinds}）——本项目记忆本就是造大脑的过程知识，"
                         f"占比高≠灌水；零唤起 meta {mb.get('zero_use_meta', 0)} 条 vs "
                         f"非 meta {mb.get('zero_use_non_meta', 0)} 条，持平即健康")
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
        ok, valid_until = _norm_date(arguments.get("valid_until"), "valid_until")
        if not ok:
            return valid_until
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
                    conf = f" ⚠️疑似互斥#{'/#'.join(str(i) for i in m['_conflict_with'])}" if m.get("_conflict_with") else ""
                    lines.append(f"{flag} #{m['id']} [{brain.KIND_CN.get(m['kind'], m['kind'])}] {m['content'][:120]}{conf}")
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
        # Y2 自动化（开工推送第五段）：启用的 heartbeat 触发器在这里自动跑——
        # 「每次开工顺手检查一遍」这类事不该依赖每个 agent 自觉，交给大脑记着。
        # 三道防线：enabled 默认关 / 冷却默认 12h / 单个动作时间预算 20s（超时丢弃不阻塞心跳）。
        try:
            fired = brain.trigger_eval(root, "heartbeat",
                                       {"agent": agent, "project": project, "note": note},
                                       budget_sec=20)
            # Y2 due 类：有到期的事（决策到回顾日/任务超期）才跑，没有就静默
            fired += brain.trigger_eval(root, "due", {"agent": agent, "project": project},
                                        budget_sec=20)
        except Exception:  # noqa: BLE001  自动化绝不能把心跳搞挂
            fired = []
        fired = [f for f in fired if f.get("status") in ("fired", "failed")]  # 冷却中的不报，免噪音
        if fired:
            al = ["", "[自动化] 本轮心跳触发的动作："]
            for f in fired:
                mark = "√" if f["status"] == "fired" else "×"
                al.append(f"  {mark} #{f['id']} {f['name']}：{(f.get('text') or '')[:90]}")
            al.append("  管理：hub_trigger action=list/fire/enable/disable")
            out = out + "\n".join(al)
        return out

    if name == "hub_trigger":
        action = str(arguments.get("action", "list")).strip()
        if action == "list":
            rows = brain.trigger_list(root, str(arguments.get("on_event", "")).strip())
            if not rows:
                return ("（还没有触发器。建一条：hub_trigger action=add name=… on_event=heartbeat "
                        "action_type=playbook action_args={name:onboard}——注意建完默认关闭，"
                        "确认无误再 action=enable id=…）")
            lines = [f"触发器 {len(rows)} 条（启用的在前）："]
            for t in rows:
                state = "启用" if int(t.get("enabled") or 0) else "停用"
                cond = json.dumps(t.get("condition") or {}, ensure_ascii=False)
                aargs = json.dumps(t.get("action_args") or {}, ensure_ascii=False)
                lines.append(f"- #{t['id']} [{state}] {t['name']}：{t['on_event']} → {t['action_type']}"
                             f"（冷却 {t['cooldown_min']} 分钟 · 已触发 {t['fire_count']} 次"
                             f" · 上次 {(t.get('last_fired') or '从未')[:16]}）")
                if cond != "{}":
                    lines.append(f"    条件 {cond}")
                if aargs != "{}":
                    lines.append(f"    动作 {aargs}")
            return "\n".join(lines)
        if action == "add":
            err, tid = brain.trigger_add(
                root, str(arguments.get("name", "")).strip(),
                str(arguments.get("on_event", "heartbeat")).strip() or "heartbeat",
                str(arguments.get("action_type", "notify")).strip() or "notify",
                arguments.get("action_args") if isinstance(arguments.get("action_args"), dict) else {},
                arguments.get("condition") if isinstance(arguments.get("condition"), dict) else {},
                arguments.get("cooldown_min"), str(arguments.get("agent", "")).strip())
            if err:
                return "错误：" + err
            return (f"触发器已建 #{tid}（默认**停用**——确认无误后 action=enable id={tid} 才生效）。"
                    f"先试跑：hub_trigger action=fire id={tid} force=true")
        if action in ("enable", "disable"):
            return brain.trigger_set_enabled(root, arguments.get("id"), action == "enable")
        if action == "del":
            return brain.trigger_del(root, arguments.get("id"))
        if action == "fire":
            res = brain.trigger_fire(root, arguments.get("id"),
                                     {"agent": str(arguments.get("agent", "")).strip(),
                                      "project": str(arguments.get("project", "")).strip()},
                                     force=bool(arguments.get("force")))
            if res.get("error"):
                return "错误：" + res["error"]
            mark = "√" if res["status"] == "fired" else "×"
            return f"{mark} 触发器 #{res['id']} {res['name']}：{res['text']}"
        return "错误：action 只支持 list/add/enable/disable/del/fire"

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

    if name == "hub_error_close":
        agent = str(arguments.get("agent", "unknown")).strip() or "unknown"
        try:
            eid = int(arguments.get("error_id"))
        except (TypeError, ValueError):
            return ("错误：error_id 必须是整数 id（如 12）——取自 hub_list_errors 输出的 #N。"
                    "先调 hub_list_errors(status=\"open\") 核对，不要猜。")
        if eid <= 0:
            return "错误：error_id 必须是正整数（如 12）。先调 hub_list_errors(status=\"open\") 核对未关闭错误的 id。"
        cur = next((e for e in brain.error_list(root) if e["id"] == eid), None)
        if cur is None:
            return (f"错误：未找到 id={eid} 的错误。当前全部错误登记用 hub_list_errors 查看"
                    "（输出行首 #N 即 id，新在前）。")
        if cur["status"] == "fixed":
            return f"错误 #{eid} 已是关闭状态，无需重复关（hub_list_errors 可见全部）。"
        note = str(arguments.get("note", "")).strip()[:200]
        err = brain.error_set_status(root, eid, "fixed")
        if err:
            return f"错误：{err}"
        brain.journal_add(root, agent, "关闭错误", target=f"errors#{eid}", note=note)
        return f"已关闭错误 #{eid}（{agent}）" + (f"：{note}" if note else "")

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
        if h.get("conflict_pair_count"):
            lines.append(f"- ⚠️ 疑似互斥记忆 {h['conflict_pair_count']} 对（一条像在修正另一条，"
                         "agent 可能拿矛盾的事实干活）：")
            for d in h["conflict_pairs"][:3]:
                lines.append(f"  ◆ #{d['a']}~#{d['b']}：{d['content_a'][:36]} ↔ {d['content_b'][:36]}")
            lines.append("  裁决：hub_memory_write supersedes=旧id 取代，或 hub_consolidate 看整合提案")
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
        out = tip + "\n对方用 hub_signals 查收；未读时其下次心跳会自动提醒"
        # Y2：发信号即评估 signal 触发器（例如「有人提到发布就自动播一下」）
        try:
            fired = brain.trigger_eval(root, "signal",
                                       {"agent": str(arguments.get("to_agent", "")),
                                        "from_agent": str(arguments.get("agent", "")),
                                        "project": str(arguments.get("project", "")),
                                        "content": str(arguments.get("content", "")),
                                        "keyword": str(arguments.get("content", ""))}, budget_sec=20)
        except Exception:  # noqa: BLE001
            fired = []
        for f in fired:
            if f.get("status") == "cooled":
                continue
            mark = "√" if f["status"] == "fired" else "×"
            out += f"\n[自动化] {mark} #{f['id']} {f['name']}：{(f.get('text') or '')[:90]}"
        return out

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
            if not tid:
                return ("错误：查看检查点时间线需要 task_id"
                        "（检查点按任务维度组织：hub_checkpoint(action=list, task_id=<id>)）")
            rows = brain.checkpoint_list(root, tid)
            if not rows:
                return f"（任务 #{tid} 还没有检查点。执行中随手 hub_checkpoint(action=save, content=...)）"
            lines = [f"检查点时间线 {len(rows)} 条（新在上）："]
            for c in rows:
                tag = "自动" if c["kind"] == "auto_claim" else "手写"
                lines.append(f"- #{c['id']} {c['ts'][5:16]} [{tag}] {c['agent']}：{c['content'][:80]}")
            return "\n".join(lines)
        if action == "latest":
            if not tid:
                return "错误：查看最新检查点需要 task_id（hub_checkpoint(action=latest, task_id=<id>)）"
            cp = brain.checkpoint_latest(root, tid)
            if not cp:
                return f"（任务 #{tid} 还没有检查点）"
            return f"任务 #{tid} 最新检查点（{cp['ts'][5:16]} {cp['agent']}）：{cp['content']}"
        return f"错误：action 只支持 save/list/latest（收到 {action!r}）"

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

    if name == "hub_export":
        action = str(arguments.get("action", "export")).strip()
        if action == "export":
            try:
                lim = int(arguments.get("limit", 5000))
            except (TypeError, ValueError):
                lim = 5000
            err, path = brain.export_all(root, str(arguments.get("fmt", "markdown")), lim)
            return f"错误：{err}" if err else f"已导出：{path}"
        if action == "backup":
            err, path = brain.backup_db(root, str(arguments.get("dest", "")))
            return f"错误：{err}" if err else f"已备份：{path}（单一 .zip：一致性库快照+磁盘项目文件树，可整盘还原）"
        if action == "backups":
            rows = brain.backup_list(root)
            if not rows:
                return "（还没有备份。hub_export action=backup 立即备份一次）"
            lines = [f"备份 {len(rows)} 份（新在前）："]
            for b in rows:
                lines.append(f"- {b['mtime']}  {b['size'] // 1024}KB  {b['path']}")
            lines.append("校验：hub_export action=verify path=…（只读校验，不恢复）")
            return "\n".join(lines)
        if action == "verify":
            err, summary = brain.backup_verify(root, str(arguments.get("path", "")))
            return f"错误：{err}" if err else summary
        return f"错误：action 只支持 export/backup/backups/verify（收到 {action!r}）"

    if name == "hub_history":
        h = brain.history_of(root, str(arguments.get("kind", "record")),
                             arguments.get("id"))
        if not h["item"]:
            return f"错误：{h['kind']} #{arguments.get('id')} 不存在"
        lines = [f"{h['kind']} #{h['id']} 变更史 {len(h['timeline'])} 条（时间升序；"
                 "来源 journal 流水 / supersede 取代链 / 条目自身时点）："]
        for t in h["timeline"]:
            stamp = (t["ts"] or "")[:16] or "—"
            detail = f"：{t['detail']}" if t["detail"] else ""
            lines.append(f"- {stamp} [{t['source']}] {t['event']}{detail}")
        return "\n".join(lines)

    if name == "hub_playbook":
        action = str(arguments.get("action", "list")).strip()
        if action == "list":
            rows = brain.playbook_list(root)
            if not rows:
                return "（还没有流程模板。内置模板首次调用会自动补种；也可 action=create 自建）"
            lines = [f"流程 {len(rows)} 套（跑：hub_playbook action=run name=… project=…）："]
            for p in rows:
                tag = "内置" if int(p.get("builtin") or 0) else "自定义"
                last = p.get("last_status") or "未跑过"
                lines.append(f"- [{tag}] {p['name']} — {p['title']}"
                             f"（{p['n_steps']} 步 · 成功跑过 {p['n_done']} 次 · 最近：{last}）")
                if p.get("desc"):
                    lines.append(f"    {p['desc']}")
            lines.append("先看步骤：action=show name=…；先看计划：action=run name=… plan=true")
            return "\n".join(lines)
        if action == "show":
            nm = str(arguments.get("name", "")).strip()
            pb = brain.playbook_get(root, nm)
            if not pb:
                return f"错误：流程「{nm}」不存在。hub_playbook action=list 看清单"
            m = pb["meta"]
            lines = [f"流程 {m['name']} — {m['title']}（{'内置模板' if m.get('builtin') else '自定义'}）"]
            if m.get("desc"):
                lines.append(m["desc"])
            for s in pb["steps"]:
                a = json.dumps(s.get("args") or {}, ensure_ascii=False)
                a = a if a != "{}" else ""
                note = f"｜{s['note']}" if s.get("note") else ""
                lines.append(f"  {s['seq']}. {s['title']}  [{s['action']}]  {a}{note}")
            lines.append("跑：hub_playbook action=run name=" + m["name"] + " project=…（建议先 plan=true）")
            return "\n".join(lines)
        if action == "create":
            nm = str(arguments.get("name", "")).strip()
            steps = arguments.get("steps")
            if not isinstance(steps, list):
                return "错误：steps 必填且为数组（[{key,title,action,args,note}]，action 见 action=list 说明）"
            err, n = brain.playbook_upsert(root, nm, str(arguments.get("title", "")),
                                           str(arguments.get("desc", "")), steps,
                                           str(arguments.get("agent", "")))
            return f"错误：{err}" if err else \
                f"流程已保存：{nm}（{n} 步）。跑：hub_playbook action=run name={nm}"
        if action == "run":
            res = brain.playbook_run(root, str(arguments.get("name", "")).strip(),
                                     str(arguments.get("project", "")).strip(),
                                     str(arguments.get("agent", "")).strip(),
                                     str(arguments.get("target", "")).strip(),
                                     dry_run=bool(arguments.get("plan")))
            if res.get("error"):
                return "错误：" + res["error"]
            if res.get("dry_run"):
                lines = [f"预演（未执行）：{res['playbook']} — {res['title']}"]
                for s in res["steps"]:
                    a = json.dumps(s.get("args") or {}, ensure_ascii=False)
                    lines.append(f"  {s['seq']}. {s['title']}  [{s['action']}]  {'' if a == '{}' else a}")
                lines.append("真跑：去掉 plan（建议带 project/agent 便于留痕）")
                return "\n".join(lines)
            return _fmt_playbook_run(res)
        if action == "resume":
            res = brain.playbook_resume(root, arguments.get("run_id"),
                                        str(arguments.get("agent", "")).strip(),
                                        str(arguments.get("target", "")).strip())
            return "错误：" + res["error"] if res.get("error") else _fmt_playbook_run(res)
        if action == "runs":
            ok, lim = _as_int(arguments, "limit", 10)
            if not ok:
                return lim
            rows = brain.playbook_runs(root, str(arguments.get("name", "")).strip(), lim)
            if not rows:
                return "（还没有执行记录。hub_playbook action=run name=… 跑一次）"
            lines = [f"执行历史 {len(rows)} 条（新在前）："]
            for r in rows:
                lines.append(f"- #{r['id']} {r['playbook']} [{r['status']}] {r['summary']}"
                             f"（{r['project'] or '无项目'} · {r['agent'] or '无'} · {r['started'][:16]}）")
            lines.append("看分步：action=resume run_id=…（继续跑）／action=abort run_id=…（中止）")
            return "\n".join(lines)
        if action == "abort":
            return brain.playbook_abort(root, arguments.get("run_id"))
        return "错误：action 只支持 list/show/create/run/resume/runs/abort"

    if name == "hub_graph":
        action = str(arguments.get("action", "stats")).strip()
        if action == "build":
            s = brain.graph_build(root)
            return (f"结构边已重建：标签边 {s['tag']} 条（覆盖 {s['memories']} 条记忆）· "
                    f"项目边 {s['in']} 条（覆盖 {s['records']} 条记录）。"
                    "（双链 mention 与手工 link 边由写入侧维护，本次不动）")
        if action == "neighbors":
            ok, lim = _as_int(arguments, "limit", 30)
            if not ok:
                return lim
            res = brain.graph_neighbors(root, str(arguments.get("node", "")), lim)
            if res.get("error"):
                return "错误：" + res["error"]
            lines = [f"节点 {res['node']} 的邻居：出边 {len(res['out'])} · 入边 {len(res['in'])}"]
            for r in res["out"][:12]:
                lines.append(f"  → {r['to_kind']}:{r['to_ref']} [{r['kind']}]"
                             + (f"｜{r['snippet']}" if r.get("snippet") else ""))
            for r in res["in"][:12]:
                lines.append(f"  ← 来自 {r['from_kind']}:{r['from_id']} [{r['kind']}]"
                             + (f"｜{r['snippet']}" if r.get("snippet") else ""))
            if not res["out"] and not res["in"]:
                lines.append("  （孤立节点：没跟任何东西相连——要么先 action=build，要么它真的是孤岛）")
            return "\n".join(lines)
        if action == "related":
            ok, lim = _as_int(arguments, "limit", 10)
            if not ok:
                return lim
            rows = brain.graph_related(root, str(arguments.get("node", "")), lim)
            if not rows:
                return ("（图上没有关联条目。可能该节点本身孤立——先 action=build 重建结构边；"
                        "或换条记录试）")
            lines = ["图上最相关的条目（二跳内，显式双链加权）："]
            for r in rows:
                proj = f"[{r['project']}] " if r.get("project") else ""
                lines.append(f"- {r['node']}（相关度 {r['score']}）{proj}{r['snippet']}")
            return "\n".join(lines)
        if action == "path":
            ok, depth = _as_int(arguments, "max_depth", 4)
            if not ok:
                return depth
            res = brain.graph_path(root, str(arguments.get("from_node", "")),
                                   str(arguments.get("to_node", "")), depth)
            if res.get("error"):
                return "错误：" + res["error"]
            if not res.get("found"):
                return f"未找到路径：{res.get('hint', '')}"
            return f"路径（{res['depth']} 跳）：" + " → ".join(res["path"])
        if action == "isolated":
            ok, lim = _as_int(arguments, "limit", 20)
            if not ok:
                return lim
            rows = brain.graph_isolated(root, lim)
            if not rows:
                return "（没有孤岛——所有条目都跟别的东西有连接）"
            lines = [f"孤岛 {len(rows)} 条（无边相连，检索最难顺带唤起它们）："]
            for r in rows:
                proj = f"[{r['project']}] " if r.get("project") else ""
                sug = f"｜建议标签：{'、'.join(r['suggested_tags'])}" if r.get("suggested_tags") else ""
                lines.append(f"- {r['node']} {proj}{r['snippet']}{sug}")
            lines.append("处理：给它补标签（记忆）或建 [[双链]]（记录），再 action=build 重建边")
            return "\n".join(lines)
        if action == "clusters":
            ok, lim = _as_int(arguments, "limit", 8)
            if not ok:
                return lim
            rows = brain.graph_clusters(root, lim)
            if not rows:
                return "（还没有成规模的簇。先 action=build 重建结构边，或库里条目还太少）"
            lines = [f"知识簇 {len(rows)} 个（按规模；≥2 个条目才算簇）："]
            for g in rows:
                lines.append(f"- {g['size']} 个条目：" + "；".join(g["sample"][:2]))
            lines.append("孤岛=没进任何簇的条目，检索最难顺带唤起它们（action=stats 看数量）")
            return "\n".join(lines)
        st = brain.graph_stats(root)
        if st.get("error"):
            return "错误：" + st["error"]
        bk = "、".join(f"{k} {v}" for k, v in st["by_kind"].items()) or "无"
        return (f"知识图谱体检：边 {st['edges']} 条（{bk}）｜节点 记录 {st['nodes']['record']} · "
                f"记忆 {st['nodes']['memory']}｜已连条目 {st['linked']} · 孤岛 {st['isolated']} · "
                f"最大簇 {st['largest_cluster']}\n"
                "看关联：action=neighbors/related/path；看簇：action=clusters；重建结构边：action=build")

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

    return f"错误：未知工具 {name}（用 hub_search_tools 搜索可用工具，如 hub_search_tools(keyword=\"错误\")）"


TOOLS = [
    {"name": "hub_list_projects", "description": "列出 AgentHub 全部项目（名称/最近活动/记录数）。想确认有没有某个项目、或拿准确目录名时用；项目名已知时直接 hub_get_project，不必先列表。例：hub_list_projects()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_get_project", "description": "查看某项目的路径、文件与最近工作记录。项目目录名不确定时先用 hub_list_projects 查准确名（「对象-问题」结构）——猜错名字只会得到报错。例：hub_get_project(project=\"AgentHub-多agent文件统一管理\")",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string", "description": "项目目录名（须含连字符，如 AgentHub-多agent文件统一管理）"}}, "required": ["project"]}},
    {"name": "hub_create_project", "description": "按规范新建项目目录（「对象-问题」命名，含 input/output 与工作记录模板）。要开新项目时用；项目可能已存在时先 hub_list_projects 查，别建重复目录。例：hub_create_project(project=\"KVK训枪-桌面图标异常\")",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "项目目录名，须含连字符"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["project"]}},
    {"name": "hub_log_work", "description": "向项目工作记录追加一条（铁律：所有 agent 干完活必须调用，否则下次的自己接不上）。content 模板：目的/做了什么/验证结果/如何回滚；含「决策」段自动归为 decision。例：hub_log_work(project=\"AgentHub-多agent文件统一管理\", agent=\"workbuddy\", content=\"目的：…做了什么：…验证：…回滚：…\")",
     "inputSchema": {"type": "object",
                     "properties": {"project": {"type": "string", "description": "项目目录名（「对象-问题」结构，须含连字符）"},
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
    {"name": "hub_search", "description": "跨项目全文搜索工作记录/记忆/文件名。找历史踩坑/决策/记录时用——keyword 必须带具体词（项目名/组件名/错误类型，如 \"CS2 卡死 显存\"），空泛词（\"问题\"/\"工具\"）只会召回噪声，单字大概率零命中；中文长串零命中时自动 2-gram 放宽。命中行带 #id，精读用 hub_get_record。例：hub_search(keyword=\"ops_run 执行异常\")",
     "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string", "description": "具体关键词（项目名/组件名/错误类型），2-4 个词组合效果最好"}}, "required": ["keyword"]}},
    {"name": "hub_note", "description": "记入人的随记（顿悟/想法/摘录，主人第一人称思想）——主人说\"记一下：XXX\"时调用；content 必填，tags/link/mood 可选。随记与工作经验（memories）分层但检索互通。例：hub_note(content=\"主人说：记一下…\", tags=\"顿悟\")",
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
    {"name": "hub_get_rules", "description": "读取团队协作规范（目录命名/记录格式/铁律）。第一次接触本大脑、或遇到「该不该这么做」的规范问题时必读——答案都在这，不要凭猜。例：hub_get_rules()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_memory_read", "description": "读取大脑记忆库（五类：环境事实 fact/用户偏好 preference/踩坑 lesson/项目进展 project/随手记 note）。开工对齐背景、查用户偏好/踩坑时用；带 query 必须带具体词（如 \"落盘 铁律\"），空查只返回置顶+最近。例：hub_memory_read(query=\"落盘 铁律\", kind=\"lesson\")",
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
    {"name": "hub_memory_write", "description": "写入一条大脑记忆，供所有 agent 检索。有跨会话价值的结论（踩坑/偏好/环境事实/项目进展）立即沉淀；随手琐事别写（稀释检索）。kind 建议明确；supersedes 可取代旧记忆（旧条退出推送但可追溯）。例：hub_memory_write(content=\"…\", kind=\"lesson\", tags=\"agenthub,UX\")",
     "inputSchema": {"type": "object",
                     "properties": {"content": {"type": "string", "description": "记忆正文（结论先行，标注「取代记忆#N」时写清错在哪）"},
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
    {"name": "hub_heartbeat", "description": "会话心跳：登记自己正在哪个项目干活。同项目有其他 agent 活跃时会收到撞车预警，长任务开工前先调用（开工三连之一）；短任务可不调。例：hub_heartbeat(agent=\"workbuddy\", project=\"AgentHub-多agent文件统一管理\", note=\"在改 MCP 描述\")",
     "inputSchema": {"type": "object",
                     "properties": {"agent": {"type": "string", "description": "你的 agent 名"},
                                    "project": {"type": "string", "description": "正在工作的项目名"},
                                    "note": {"type": "string", "description": "一句话说明在做什么"}},
                     "required": ["agent"]}},
    {"name": "hub_report_error", "description": "登记错误/踩坑（含回滚方式），用户与其他 agent 可在流水页查询。出错/受阻/踩坑时主动调用；修完后必须用 hub_error_close 关闭，别留僵尸。例：hub_report_error(title=\"部署漂移\", detail=\"改了源码没 deploy\", undo=\"cli.py deploy\")",
     "inputSchema": {"type": "object",
                     "properties": {"title": {"type": "string", "description": "一句话错误摘要"},
                                    "detail": {"type": "string", "description": "现象/原因/过程"},
                                    "project": {"type": "string", "description": "相关项目名，可空"},
                                    "undo": {"type": "string", "description": "如何回滚/撤销"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["title"]}},
    {"name": "hub_list_errors", "description": "查询错误登记（status=open/fixed）。排查历史问题、确认 bug 修没修时用；open 列表有堆积=有关闭动作没做（修完用 hub_error_close 关）。例：hub_list_errors(status=\"open\")",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["open", "fixed"], "description": "过滤状态；缺省返回全部"}}}},
    {"name": "hub_error_close", "description": "关闭一条已修复的错误登记（open→fixed）。修完 bug 后必须调用——只登记不关闭会让未关闭列表堆积僵尸错误、淹没真故障（本大脑曾积压 8 条僵尸）。没修完别关（留着提醒后来人）；error_id 取自 hub_list_errors 输出的 #N，不要猜。例：hub_error_close(error_id=12, note=\"整数入参坏类型已修\")",
     "inputSchema": {"type": "object",
                     "properties": {"error_id": {"type": "integer", "description": "错误 id（hub_list_errors 输出里的 #N，如 12）"},
                                    "note": {"type": "string", "description": "怎么修的/为什么关（一句话，进流水可追溯）"},
                                    "agent": {"type": "string", "description": "你的 agent 名（进流水）"}},
                     "required": ["error_id"]}},
    {"name": "hub_undo", "description": "撤销你（agent）最近一条经 hub_log_work 写入的记录段（原文自动备份，操作进流水）。写错记录/写重了时用；只能撤自己的，别人的记录不许动。例：hub_undo(agent=\"workbuddy\")",
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
    {"name": "hub_list_mcps", "description": "列出本机各 agent 已配置的 MCP 服务器（名称/命令/状态）。核对谁接了哪些 MCP、接入是否生效时用。例：hub_list_mcps()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_bootstrap_status",
     "description": "各 agent 的接入状态（只读）：引导规则是否已注入 + MCP 是否已配 agenthub + 送达通道。用于核对「谁还没接上大脑」",
     "inputSchema": {"type": "object", "properties": {"agent": {"type": "string", "description": "可选，只看某个 agent"}}}},
    {"name": "hub_list_agents", "description": "列出注册 agent 身份表（在线状态/累计记录/心跳次数/最近活跃）。判断谁在线、谁还没接上大脑时用；不需要历史轨迹时不必调。例：hub_list_agents()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_list_todos", "description": "列出工作记录里的待办/承诺线索（后续/待验证/下一步…句式）。开工或收尾时看有没有欠账；处理完的用 hub_todo_done 勾销闭环。例：hub_list_todos()",
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
    {"name": "hub_export",
     "description": "X3 导出与备份：action=export 全库导出（fmt=markdown 人读 / json 机读，落到 _hub/export）；action=backup 备份数据库（走 sqlite backup API，含 WAL——文件复制会丢数据）；backups 列备份；verify 只读校验备份可读（不恢复）",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string", "enum": ["export", "backup", "backups", "verify"],
                                               "description": "export 导出（缺省）/ backup 备份 / backups 清单 / verify 校验"},
                                    "fmt": {"type": "string", "enum": ["markdown", "json"], "description": "export：格式，缺省 markdown"},
                                    "limit": {"type": "integer", "description": "export：导出行上限，默认 5000"},
                                    "dest": {"type": "string", "description": "backup：目标目录（可空=默认 _hub/backup）"},
                                    "path": {"type": "string", "description": "verify：备份文件路径"}}}},
    {"name": "hub_history",
     "description": "X4 时间机器：某条记录/记忆的完整变更史（journal 流水 + supersede 取代链 + 条目自身时点，三源合并按时间升序）——复盘「这条怎么变成现在这样」",
     "inputSchema": {"type": "object",
                     "properties": {"kind": {"type": "string", "enum": ["record", "memory"], "description": "条目类型，缺省 record"},
                                    "id": {"type": "integer", "description": "条目 id"}},
                     "required": ["id"]}},
    {"name": "hub_trigger",
     "description": "Y2 自动化层：事件驱动——把「每次开工顺手检查一遍」「有人发信号就通知我」这类**本该自动的事**交给大脑，不再依赖每个 agent 自觉。on_event=heartbeat（每次心跳，受冷却约束）/signal（收到信号）/record（写了记录）；action_type=notify（心跳里提示一行）/signal（发信号）/playbook（自动跑流程）。三道防线：建完默认**停用**、冷却默认 12 小时、单个动作时间预算 20 秒（超时丢弃，绝不阻塞心跳）",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string",
                                               "enum": ["list", "add", "enable", "disable", "del", "fire"],
                                               "description": "list 清单（缺省）/ add 新建 / enable 启用 / disable 停用 / del 删除 / fire 手动触发一次"},
                                    "name": {"type": "string", "description": "add：触发器名（写清触发什么）"},
                                    "on_event": {"type": "string",
                                                 "enum": ["heartbeat", "signal", "record", "due"],
                                                 "description": "add/list：监听什么事件。heartbeat 每次心跳（受冷却约束）/ due **时点到了**才跑（决策到回顾日、任务超期；没到期项不出声）"},
                                    "action_type": {"type": "string", "enum": ["notify", "signal", "playbook"],
                                                    "description": "add：触发后做什么，缺省 notify"},
                                    "action_args": {"type": "object",
                                                    "description": "add：动作参数。playbook 需 {name:流程名, project?}；signal 需 {content, to_agent?, kind?}；notify 需 {text}"},
                                    "condition": {"type": "object",
                                                  "description": "add：触发条件 {project?, agent?, keyword?}——等值匹配，keyword 走包含；不传=不限制"},
                                    "cooldown_min": {"type": "integer",
                                                     "description": "add：冷却分钟数，默认 720（12 小时），上限 10080"},
                                    "id": {"type": "integer", "description": "enable/disable/del/fire：触发器 id"},
                                    "force": {"type": "boolean",
                                              "description": "fire：true=忽略冷却与启用状态强制试跑（调试用）"},
                                    "agent": {"type": "string", "description": "你的 agent 名"},
                                    "project": {"type": "string", "description": "fire：提供 {{project}} 变量"}}}},
    {"name": "hub_graph",
     "description": "Y3 知识图谱（关联检索）：检索按「词像不像」，图谱按「图上近不近」——一条没提到关键词但同项目同标签的记录，检索找不到，图谱能。action=stats 体检（边/节点/孤岛/最大簇）、neighbors 某节点一跳邻居、related 二跳内最相关条目（显式双链加权）、path 两节点最短路径、clusters 知识簇与孤岛、build 重建结构边（记忆→标签、记录→项目，幂等）",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string",
                                               "enum": ["stats", "neighbors", "related", "path", "clusters", "isolated", "build"],
                                               "description": "stats 体检（缺省）/ neighbors 邻居 / related 相关条目 / path 最短路径 / clusters 知识簇 / isolated 孤岛清单 / build 重建结构边"},
                                    "node": {"type": "string",
                                             "description": "neighbors/related：节点，格式 record:12 / memory:3 / tag:踩坑 / project:项目名"},
                                    "from_node": {"type": "string", "description": "path：起点节点"},
                                    "to_node": {"type": "string", "description": "path：终点节点"},
                                    "max_depth": {"type": "integer", "description": "path：最大跳数，默认 4（上限 6）"},
                                    "limit": {"type": "integer", "description": "返回条数上限"}}}},
    {"name": "hub_playbook",
     "description": "Y1 编排层：把每次都要手工重走的流程固化成**可执行**的 playbook——步骤落库、逐步真跑、失败即停、断点续跑（不再靠 agent 记忆重走，换会话也不丢进度）。内置模板：release 发布全流程 / new_project 新项目开工 / daily 每日巡检 / deliver 交付收尾 / onboard 接入体检。action=list 看清单、show 看步骤、run 真跑（先 plan=true 看计划）、resume 断点续跑、runs 历史、abort 中止、create 自建",
     "inputSchema": {"type": "object",
                     "properties": {"action": {"type": "string",
                                               "enum": ["list", "show", "create", "run", "resume", "runs", "abort"],
                                               "description": "list 清单（缺省）/ show 看步骤 / create 自建 / run 跑一次 / resume 断点续跑 / runs 执行历史 / abort 中止"},
                                    "name": {"type": "string",
                                             "description": "流程名（show/create/run 必填；runs 可按名过滤）"},
                                    "project": {"type": "string",
                                                "description": "run：项目名（流程里的 {{project}}；需要留痕的流程必填）"},
                                    "agent": {"type": "string", "description": "你的 agent 名（留痕用）"},
                                    "target": {"type": "string",
                                               "description": "run：ops 类步骤的目标目录，缺省 AgentHub 源码目录"},
                                    "plan": {"type": "boolean",
                                             "description": "run：true=只列步骤计划（渲染后的 args）不真跑。注意与工具级 dry_run 不同——dry_run 是「不落库预演」，plan 是「看流程打算做什么」"},
                                    "run_id": {"type": "integer", "description": "resume/abort：执行 id"},
                                    "limit": {"type": "integer", "description": "runs：条数，默认 10"},
                                    "title": {"type": "string", "description": "create：流程标题"},
                                    "desc": {"type": "string", "description": "create：流程说明"},
                                    "steps": {"type": "array",
                                              "description": "create：步骤数组 [{key,title,action,args,note}]；action 可用 ops_check/backup/export/log_work/memory/signal/create_project/view_follow/checkpoint/sentry/noop/manual（manual=人工步骤，跑到会停下等你 resume）",
                                              "items": {"type": "object"}}}}},
    {"name": "hub_report",
     "description": "生成反馈报告（单文件 HTML 双击即开）：mentor 式九章节——你在做什么/时间分布/做对了什么/摩擦点（每条带修复）/规则修改提案。提案需人确认，系统不自动改",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_ops_list", "description": "大脑内置检查工具箱清单（编译检查/跑回归/部署一致性/git/环境预检/大脑体检…）。跑检查前先看清单确认检查项名与 target 要求；单项执行用 hub_ops_run。例：hub_ops_list()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_ops_run", "description": "执行一项内置检查并返回精简摘要。写完代码跑 py_compile、收尾跑 regression、改完部署跑 deploy_diff/release、开工跑 env（这几项必填 target=目录路径；env/brain 等无需）。regression 约1分钟，其余只读。例：hub_ops_run(name=\"py_compile\", target=r\"D:\\agent资料\\zcode项目记录\\AgentHub-多agent文件统一管理\\AgentHub\")",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string", "description": "检查项名（先 hub_ops_list 看清单）",
                                             "enum": sorted(ops.CHECKS)},
                                    "target": {"type": "string", "description": "目标目录绝对路径（按检查项要求）"}},
                     "required": ["name"]}},
    {"name": "hub_health", "description": "大脑体检报告：规模/结晶率/待办线索/疑似重复记忆/检索活跃度+改进建议。定期自查或收尾评估时用；查单条数据（某项目/某记录）用对应工具。例：hub_health()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_user_profile", "description": "用户画像：活跃时段/记录分工/技术栈词频/用户纠正史 top——判断用户偏好与习惯时先查（零 LLM 实测统计）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_todo_done", "description": "勾销待办线索（处理完的欠账销账，闭环）——传 hub_list_todos 行里的 record_id；待办来自别人的记录也可勾（勾销留痕）。例：hub_todo_done(record_id=123)",
     "inputSchema": {"type": "object",
                     "properties": {"record_id": {"type": "integer", "description": "记录 id"},
                                    "todo": {"type": "string", "description": "待办原文"},
                                    "agent": {"type": "string", "description": "你的 agent 名"}},
                     "required": ["todo"]}},
    {"name": "hub_env_set", "description": "登记/更新本机环境配置项（网络/系统/工具/路径…结构化档案）。发现环境事实（路径变了/工具装了/端口换了）时先登记，让所有 agent 免重新探测；查询用 hub_env_list。例：hub_env_set(category=\"network\", key=\"proxy\", value=\"127.0.0.1:7890\")",
     "inputSchema": {"type": "object",
                     "properties": {"category": {"type": "string", "description": "分类（如 network/system/tool/path）"},
                                    "key": {"type": "string", "description": "配置项名（如 proxy/python_path）"},
                                    "value": {"type": "string", "description": "值（实测事实，别写猜测）"},
                                    "agent": {"type": "string"}},
                     "required": ["category", "key", "value"]}},
    {"name": "hub_env_list", "description": "浏览/搜索本机环境配置档案。动手前查环境事实（端口/路径/工具位置）免现场探测；查不到=没登记过（用 hub_env_set 补），别反复调。例：hub_env_list(category=\"network\")",
     "inputSchema": {"type": "object",
                     "properties": {"category": {"type": "string"}, "kw": {"type": "string"},
                                    "limit": {"type": "integer"}}}},
    {"name": "hub_search_files", "description": "Everything 全盘文件名搜索（毫秒级，需 Everything 在跑）。只搜文件名、不搜文件内容——搜内容先用 hub_search。例：hub_search_files(query=\"*.log\")",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "文件名关键词"},
                                    "limit": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "hub_distill", "description": "记忆蒸馏候选：content 有「目的」结论但同项目无记忆覆盖的记录（结晶流水线）。收尾时看它，确认后用 hub_memory_write 沉淀——日记会膨胀，笔记本只留精华。例：hub_distill(limit=10)",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer"}}}},
    {"name": "hub_get_record", "description": "按 id 精读一条工作记录（渐进披露三级：abstract 前200字 → outline 段头骨架 → full 全文，每级成本差一个量级）。先从 hub_search/hub_list_todos 等拿到 #id 再调；不知道 id 时不要盲调。例：hub_get_record(record_id=3262, level=\"outline\")",
     "inputSchema": {"type": "object",
                     "properties": {"record_id": {"type": "integer", "description": "记录 id，如 2037"},
                                    "level": {"type": "string", "enum": ["full", "abstract", "outline"],
                                              "description": "full=全文（缺省）；abstract=前200字；outline=段头骨架"}},
                     "required": ["record_id"]}},
    {"name": "hub_get_progress", "description": "获取所有 agent 最近的工作时间线（进度对齐）。开工三连之一：看别人在干什么、防止撞车；limit 控制条数。例：hub_get_progress(limit=15)",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "条数，默认30"}}}},
    {"name": "hub_status", "description": "大脑此刻状态（连续性层）：谁在线/活跃项目/今日变更/未关闭错误与待认领交接/置顶记忆与停滞项目/待办线索。每次开工看它对齐「现在」；历史沿革用 hub_search。例：hub_status()",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_help",
     "description": "起步指北（任何 profile 常驻可见）：当前 profile 与可见工具清单 + 发现/调用/预演/截断四件事的用法",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "hub_search_tools",
     "description": "按关键词搜索全部工具（含当前 profile 藏起的；藏起≠禁用）。找不到某个能力/忘了工具名时先来这里；已知工具名直接调，不必搜。例：hub_search_tools(keyword=\"错误\")",
     "inputSchema": {"type": "object",
                     "properties": {"keyword": {"type": "string",
                                                "description": "关键词（匹配工具名/说明子串），缺省列出全部"}}}},
    {"name": "hub_call_tool",
     "description": "调用任意工具（含被 profile 藏起的）——name + arguments 透传；写工具同样受 dry_run/confirm/审计约束。找不到某个能力时先经 hub_search_tools 发现，再用它调；已知名的工具直接 tools/call 即可。例：hub_call_tool(name=\"hub_health\", arguments={})",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string", "description": "目标工具名"},
                                    "arguments": {"type": "object", "description": "目标工具的参数对象"}},
                     "required": ["name"]}},
    {"name": "hub_audit",
     "description": "写操作审计（P3-1）：最近的写工具调用记录（谁/何时/写了什么/undo 句柄）。怀疑误写、要找 undo 句柄时查；日常只读调用不入审计。例：hub_audit(limit=10)",
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


# ---- 集中式必填校验（Round 6 / 2026-10-13）----
# 已声明 required 的工具在执行前统一校验，避免「参数名拼错 / 漏传」导致的隐晦下游错误，
# 也符合 MCP 官方与 mcpcat.io 的「执行前按 schema 校验入参」最佳实践。
# 安全豁免（见 call_tool 内注释）：agent（进程锁定身份兜底）/ hub_checkpoint（条件必填）。
_REQUIRED_EXEMPT_TOOLS = frozenset({"hub_checkpoint"})
_REQUIRED_EXEMPT_PARAMS = frozenset({"agent"})


def _effective_required(tname: str) -> list:
    """返回某工具实际需要强制的必填参数（已剔除豁免参数 agent）。"""
    for t in TOOLS:
        if t["name"] == tname:
            return [p for p in (t["inputSchema"].get("required") or [])
                    if p not in _REQUIRED_EXEMPT_PARAMS]
    return []


def _example_hint(tname: str) -> str:
    """从工具自己的描述里摘「例：…」拼进错误文案。

    错误信息三件套（外部最佳实践：tsecurity.de / agentpatterns.io）——
    什么错了 / 期望什么 / 正确输入示例。示例从 TOOLS 描述里现取，与描述同源
    不漂移；R2 起核心工具描述全部带示例，这里直接复用。
    """
    t = next((x for x in TOOLS if x["name"] == tname), None)
    if not t:
        return ""
    d = t.get("description", "")
    i = d.find("例：")
    if i < 0:
        return ""
    return " 正确示例：" + d[i + 2:].split("。")[0].strip()


def _template_nudge(content: str, rt: str) -> str:
    """R7·四段式模板软提示（2026-10-17）：work_log 缺段时只在回显点一句。

    铁律「content 含 目的/做了什么/验证结果/如何回滚」实测遵守率仅 48%
    （近 200 条 active 记录四段俱全率，缺目的 46%/缺回滚 22%）——没有守卫的
    标准必然腐烂（教训 #177）。设计：不阻断（todo/note/crystal/decision 不
    适用四段式）、不污染存储内容（提示只进工具回显）、列明缺哪段——下一条
    补上，比事后盘点便宜。measurement 见 ops 检查项 record_quality。
    """
    if rt and rt not in ("work_log", ""):
        return ""
    miss = [k for k, pat in (("目的", "目的"), ("做了什么", "做了什么"),
                             ("验证结果", "验证"), ("如何回滚", "回滚"))
            if not re.search(pat, content or "")]
    if not miss:
        return ""
    return (f"（模板提示：铁律四段=目的/做了什么/验证结果/如何回滚，本条缺"
            f"{'、'.join(miss)}段——记录已存，下一条补上）")


def _required_missing(tname: str, args: dict) -> list:
    """返回该调用缺失的必填参数名（空列表=无缺失）。

    - 豁免整工具（hub_checkpoint）：其 required=[task_id] 仅 action=save 成立（list/latest 不需要）。
    - 只判键是否存在，不判空值：空字符串可能是合法输入，且空值由下游工具自身「错误：」文案处理。
    """
    if tname in _REQUIRED_EXEMPT_TOOLS:
        return []
    req = _effective_required(tname)
    if not req:
        return []
    return [p for p in req if p not in args]


# ---- 集中式整数入参校验（Round 13 / 2026-10-13）----
# 为什么：这些字段此前是「try/except → 静默回落默认值」——模型传坏类型时参数被**悄悄忽略**，
# 模型却以为生效了（如 supersedes 传错 → 旧记忆根本没被取代；limit 传错 → 用默认条数）。
# 这类「静默错误」比报错更难发现，与 R11/R12 同一主题（入参问题必须显式可纠正）。
# 声明即契约：新增整数参数时在此登记，漂移守卫（adv_mcp.t_round13_int_params_central）
# 会扫描源码里所有 `int(arguments.get("<k>"` 直转点，未声明即失败——防止再漏。
_INT_PARAMS = {
    "hub_asset_find": ("limit",),
    "hub_audit": ("limit",),
    "hub_distill": ("limit",),
    "hub_duplicates": ("min_len",),
    "hub_env_list": ("limit",),
    "hub_error_close": ("error_id",),
    "hub_events": ("limit", "since_id"),
    "hub_export": ("limit",),
    "hub_get_progress": ("limit",),
    "hub_get_record": ("record_id",),
    "hub_handoff": ("handoff_id",),
    "hub_list_todos": ("limit",),
    "hub_memory_read": ("limit",),
    "hub_memory_write": ("supersedes",),
    "hub_next": ("limit",),
    "hub_search_files": ("limit",),
    "hub_skill_match": ("topk",),
    "hub_tasks": ("limit",),
    "hub_todo_done": ("record_id",),
}
# 豁免（均已在各自位置处理得当，不再集中拦截）：
#   · max_chars —— 协议级结果预算参数，_shape_result 刻意「非法值回落默认」的硬预算设计
#     （抄 aider：撑不住时自我降级并明确告知），不属业务语义；
#   · note_id（hub_note_read）—— schema 声明为 string，工具自身已返回
#     「错误：note_id 必须是数字（如 12）」，本就是清晰可纠正。
_INT_EXEMPT_PARAMS = frozenset({"max_chars", "note_id"})


def _bad_int_param(tname: str, args: dict):
    """返回第一个「声明为整数但不可转 int」的参数错误文案（无则 None）。空值与缺省放行。"""
    for key in _INT_PARAMS.get(tname, ()):
        if key not in args:
            continue
        v = args.get(key)
        if v is None or v == "":
            continue
        try:
            int(v)
        except (TypeError, ValueError):
            return f"错误：参数 {key} 必须是整数（收到 {str(v)[:40]!r}）"
    return None


def _as_int(arguments: dict, key: str, default: int) -> tuple:
    """读整数字段：缺省 / 空串用 default；不可转则回 (False, 清晰提示)。

    Round 11（2026-10-13）：此前 hub_graph / hub_playbook 的 limit / max_depth 直接
    `int(arguments.get(...))`——模型传坏类型（如 "abc"）会抛 ValueError，被协议层通用兜底
    渲染成「工具执行失败（ValueError）…联系管理员」：把「入参类型问题」伪装成「服务端故障」，
    模型无法自我纠正，还在错误登记里留噪声。这里统一转成可行动的「错误：参数 X 必须是整数」。
    """
    v = arguments.get(key)
    if v is None or v == "":
        return True, default
    try:
        return True, int(v)
    except (TypeError, ValueError):
        return False, f"错误：参数 {key} 必须是整数（收到 {str(v)[:40]!r}）"


_DATE_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")


def _norm_date(value, field: str) -> tuple:
    """日期入参规范化/校验（Round 12 / 2026-10-13）：回 (True, "YYYY-MM-DD") 或 (False, 提示)。

    为什么必须做：M11 决策回顾队列、任务超期（due_summary）、记忆有效期（valid_until）
    都走 SQLite 的 `date <= date('now')` —— **字典序**比较。于是：
      · '2026/01/01'（斜杠）字典序恒 > '2026-10-13' → 永远不触发；
      · '2026-2-1'（未补零）在月份 ≥10 的今天同样被静默漏掉。
    就是说日期格式稍有偏差，「到期提醒」这个功能会**静默失效**（用户永远收不到回顾提醒），
    而且没有任何报错可循。这里统一收口：可解析的（含 / . 分隔、未补零、非法日期）规范成
    YYYY-MM-DD 或给清晰错误；空值照旧放行（保持「不传行为与旧版一致」）。
    """
    s = str(value or "").strip()
    if not s:
        return True, ""
    m = _DATE_RE.match(s)
    if not m:
        return False, (f"错误：{field} 需为 YYYY-MM-DD（如 2026-10-20），"
                       f"收到 {s[:20]!r}——格式不对会让「到期提醒」静默不触发")
    y, mo, d = (int(x) for x in m.groups())
    try:
        datetime.date(y, mo, d)
    except ValueError:
        return False, f"错误：{field} 不是合法日期（收到 {s[:20]!r}）"
    return True, f"{y:04d}-{mo:02d}-{d:02d}"


def _fmt_playbook_run(res: dict) -> str:
    """playbook 执行结果排版（run/resume 共用）。

    用纯文本状态标记不用 emoji：状态要能被日志/终端/其他 agent 无损复制，
    emoji 在不同终端渲染成方框会丢语义。
    """
    head = {"done": "[完成]", "failed": "[失败]", "waiting": "[待人工]", "aborted": "[已中止]"}.get(
        res.get("status", ""), "[执行]")
    lines = [f"{head} playbook {res.get('playbook')} run #{res.get('run_id')}：{res.get('summary', '')}"]
    mark = {"done": "√", "failed": "×", "manual": "手", "pending": "·", "skipped": "-"}
    for s in res.get("steps", []):
        out = f"  → {s['output']}" if s.get("output") else ""
        lines.append(f"  [{mark.get(s['status'], '·')}] {s['seq']}. {s['title']}{out}")
    if res.get("status") == "waiting":
        lines.append("下一步：人工完成该步后 hub_playbook action=resume run_id="
                     f"{res.get('run_id')}（该步被标为已做，从后续步骤继续）")
    elif res.get("status") == "failed":
        lines.append("下一步：修掉失败项后 hub_playbook action=resume run_id="
                     f"{res.get('run_id')}（已完成的步骤不重跑，从失败那一步继续）")
    return "\n".join(lines)


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
    "hub_export", "hub_history",  # X3 导出备份 / X4 时间机器
    "hub_playbook", "hub_trigger", "hub_graph",  # Y1/Y2/Y3 编排、自动化、图谱：都是干活的日常
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
    "hub_report_error", "hub_error_close", "hub_undo", "hub_archive_project", "hub_todo_done", "hub_env_set",
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
    "hub_export",  # X3：导出/备份（写文件）
    "hub_playbook",  # Y1：跑流程会真执行动作（备份/写记录/发信号/建项目），必须过 dry_run+confirm
    "hub_trigger",  # Y2：触发器会自行发信号/跑流程，未经确认的自动化=事故源
    "hub_graph",  # Y3：build 会重写结构边（幂等派生数据），仍过闸门保持一致性
}

# B3 破坏半径分级：WRITE_TOOLS 里「不可逆 / 批量 / 跨项目影响」的高危子集。
# 判定口径（不是按名字猜，按副作用）:①不可逆或难撤销（归档/合并/隔离）②批量改写既有数据
# （重建索引/重写结构边/批量写技能索引）③会自行触发一串动作（playbook/trigger）。
# 日常低危写入（log_work/note/memory_write/heartbeat/checkpoint/claim/task_* 等）不在其中——
# 给它们也上严格确认只会把 agent 的每次心跳都卡住，是负优化。
HIGH_BLAST_TOOLS = {
    "hub_archive_project",  # 归档项目：整项目退出活跃视图
    "hub_consolidate",      # 合并记忆组：改写既有记忆内容（不可逆）
    "hub_quarantine",       # 隔离：把可疑内容移出检索
    "hub_playbook",         # 跑流程：会真执行一串动作（备份/写记录/发信号/建项目）
    "hub_trigger",          # 自动化：未经确认的自动触发=事故源
    "hub_graph",            # build 重写全量结构边
    "hub_flush_index",      # 重建索引
    "hub_undo",             # 撤销：恢复旧状态，本身即写且会覆盖当前状态
    "hub_export",           # 导出/备份：写磁盘文件
}

# 严格确认模式（B3，默认关闭=保持历史行为「不传 confirm 即执行」）：
# 现状是「默认执行」而非「默认确认」——直接翻转默认会打断 6 个接入 agent 的日常写入
# （实测回归里高危工具 60+ 处调用全都不带 confirm），故做成可开关而非硬改默认。
# 开启：环境变量 AGENTHUB_STRICT_CONFIRM=1 → 高危工具必须显式 confirm=true 才执行。
def _strict_confirm() -> bool:
    return str(os.environ.get("AGENTHUB_STRICT_CONFIRM", "")).strip().lower() not in ("", "0", "false", "no", "off")


def _input_required(mid, text: str) -> dict:
    """return-and-resume 响应（P3-1 契约单一来源）：服务端不阻塞等待，也不主动回调。"""
    return {"jsonrpc": "2.0", "id": mid,
            "result": {"resultType": "input_required", "isError": False,
                       "content": [{"type": "text", "text": text}]}}


def _log_call(root, tname: str, args: dict, t0: float, ok: bool = True, err_kind: str = "") -> None:
    """B8 结构化可观测：每次工具调用落一行 JSONL（每行 = 一条结构化事件，便于 grep/jq/后续聚合）。
    字段：ts / tool / ms / ok / err_kind / agent / root。
    默认**关闭**（AGENTHUB_LOG 未设 → 函数首行即返回，零开销、零噪音）；
    设 AGENTHUB_LOG=<文件路径> 才落盘。为什么不默认开：常驻 server 写日志会无上限增长且
    拖慢每次调用，而绝大多数场景不需要——按需开、用完关，才是本地优先工具该有的默认。
    铁律：观测绝不能影响主链路——任何失败一律静默（不抛、不写 stdout、不污染协议流）。"""
    path = os.environ.get("AGENTHUB_LOG", "").strip()
    if not path:
        return
    try:
        rec = {"ts": datetime.datetime.now().isoformat(timespec="milliseconds"),
               "tool": tname, "ms": round((time.perf_counter() - t0) * 1000, 2),
               "ok": bool(ok), "err_kind": err_kind,
               "agent": str(args.get("agent") or ""), "root": str(root)}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


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


# ---------------------------------------------------------------- MCP 工具 annotations（C1）
# 为什么要补：MCP 规范里 host（客户端）靠 annotations 决定确认 UX——只读工具放行、
# destructiveHint=true 的弹确认。**省略 annotations 会让 host 退回保守默认（每次都问），
# 用户烦了就直接把 server 关掉**（menuagentic《Building MCP Servers in Practice》把它列为
# 首个常见错误）。这也是上一轮 B3 的**正解**：确认该由 host 按元数据决定，而不是服务端
# 自己造 AGENTHUB_STRICT_CONFIRM 开关去改默认（那会打断 6 个 agent 的日常写入）。
# 只写 true 的项（false 是规范默认值，写出来只会平白增大 schema 体积）。
# 三个集合都从既有单一来源派生，不新增第二份清单：
#   readOnlyHint     = 不在 WRITE_TOOLS（不改数据）
#   destructiveHint  = HIGH_BLAST_TOOLS（不可逆/批量/跨项目）
#   idempotentHint   = 重复调用无额外影响（UPSERT/重建/覆盖写）
IDEMPOTENT_TOOLS = {
    "hub_skill_sync",       # UPSERT 技能索引，重跑覆盖不新增
    "hub_graph",            # build 重写结构边（幂等派生数据）
    "hub_flush_index",      # 重建语义索引
    "hub_checkpoint",       # 同 task_id 覆盖保存
    "hub_env_set",          # 同键覆盖
    "hub_view",             # 关注/取关重复设置结果一致
    "hub_skill_stats",      # 重算统计量
    "hub_error_close",      # 重复关闭返回 0（已关无副作用）
    "hub_export",           # 同参数重跑产出等价备份
    "hub_sentry",           # 巡检+广播，重跑是刷新而非叠加
}
# openWorldHint（是否触达本机之外的开放世界）：本大脑是**本地优先、零依赖**，全部 69 个工具
# 经核查均不访问外部网络（hub_asset_find 查的是本地台账，v2.40 后不再爬 accat.top），
# 故**刻意一个都不标**——标了反而误导 host 去做额外的外网风险提示。
for _t in TOOLS:
    _ann = {}
    if _t["name"] not in WRITE_TOOLS:
        _ann["readOnlyHint"] = True
    if _t["name"] in HIGH_BLAST_TOOLS:
        _ann["destructiveHint"] = True
    if _t["name"] in IDEMPOTENT_TOOLS:
        _ann["idempotentHint"] = True
    if _ann:
        _t["annotations"] = _ann


# ---------------------------------------------------------------- MCP 协议层

def _shape_result(text: str, arguments: dict) -> tuple:
    """结果契约（P3-1）：默认预算内原样返回；超预算 → 截断 + 文末明确标注 + structuredContent。
    max_chars=0 关闭截断（正常输出保持原样）；非法值回落默认（撑不住时自我降级并明确告知，抄 aider 硬预算）。

    响应体积双封顶（2026-10-13 缺陷自查补，来源：MCP Dev Summit / codex.danielvaughan 把
    stdio 管道缓冲死锁列为头号运维脆弱点）：structuredContent.full_text 内联 ≤ STRUCTURED_FULL_CAP；
    max_chars<=0 时文本本身 ≤ HARD_MAX_CHARS（仅拦病理性失控输出，正常「不截断」契约不变）。
    """
    try:
        budget = (int(arguments.get("max_chars"))
                  if arguments.get("max_chars") is not None else DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        budget = DEFAULT_MAX_CHARS
    total = len(text)
    # 显式关闭截断但输出失控 → 兜底硬上限（正常 <64K 不受影响）
    if budget <= 0:
        if total <= HARD_MAX_CHARS:
            return text, None
        shown = HARD_MAX_CHARS
        capped_full = text[:STRUCTURED_FULL_CAP]
        meta = {"total": total, "shown": shown, "truncated": True,
                "reason": "hard_cap", "cursor": shown}
        foot = (f"\n…（响应硬上限：原文 {total} 字符（远超正常用量，疑似失控/异常输出），"
                f"此处仅显示前 {shown}；完整内容过大未全量内联，请缩小范围或分页获取）")
        return text[:shown] + foot, {"meta": meta, "full_text": capped_full}
    if total <= budget:
        return text, None
    meta = {"total": total, "shown": budget, "truncated": True, "cursor": budget}
    if total <= STRUCTURED_FULL_CAP:
        full = text
        foot = (f"\n…（已截断：原文 {total} 字符，此处显示前 {budget}；完整内容见 "
                "structuredContent.full_text，或传 max_chars=0 关闭截断）")
    else:
        full = text[:STRUCTURED_FULL_CAP]
        foot = (f"\n…（已截断：原文 {total} 字符，此处显示前 {budget}；完整内容过长"
                f"（>{STRUCTURED_FULL_CAP}）未全量内联，请缩小范围或分页获取，"
                "或传 max_chars=0 关闭截断）")
    return text[:budget] + foot, {"meta": meta, "full_text": full}


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
    except Exception as e:  # noqa: BLE001 审计失败不影响主流程，但要留痕（2026-10-13 审计补）
        try:
            brain.error_add(root, "system", f"写审计留痕失败（{tname}）",
                            f"{type(e).__name__}: {e}"[:300],
                            str(arguments.get("project") or ""))
        except Exception:  # noqa: BLE001
            pass


def _is_error_text(text: str) -> bool:
    """判断工具返回文本是否为业务失败。

    约定：全代码库 50+ 个业务失败站点统一以「错误：」开头返回，成功文案从不以此开头
    （已核实无碰撞）。据此把业务失败正确标记为 isError=True，让 MCP 客户端/模型看到失败、
    能够自我纠正——否则 isError 恒为 False，模型把失败当成功、无法重试，正是 MCP 官方
    （modelcontextprotocol.io / wati.io）明确反对的反模式。
    """
    return isinstance(text, str) and text.startswith(("错误：", "错误:"))


def handle_message(msg: dict, root: str) -> dict | None:
    """处理一条 JSON-RPC 请求，返回响应；通知返回 None。"""
    # 防御：畸形请求（非 dict / 空 / 数组）不能让整个 stdio 服务崩——回 JSON-RPC Invalid Request。
    # arxiv MCP 故障分类（2606.05339）指出"请求处理前未做校验"是高发故障源；客户端发来坏帧
    # 时应优雅拒绝而非抛未捕获异常（未捕获异常会断连、工具全失）。
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request: expected a JSON object"}}
    method = msg.get("method", "")
    mid = msg.get("id")
    is_notification = mid is None
    params = msg.get("params") or {}

    # 建表责任下沉到公共分发入口：stdio 的 serve() 与原 REST api.py 都经 handle_message 分发，
    # 此前 ensure_schema 只挂在 serve() 里，api.py 直接调 handle_message 会绕过——
    # 全新根目录首请求即 no such table（代码部署了但真实库还没建表）。现每请求幂等自愈，两端共用同一道守卫。
    brain.ensure_schema(root)

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
            return _input_required(mid,
                                   f"调用未执行：{tname} 是写操作。人工确认后重发并传 confirm=true。"
                                   "（return-and-resume：服务端不阻塞等待，也不会主动回调。）")
        # B3 破坏半径分级：严格模式下高危工具必须显式 confirm=true 才执行（默认关闭，
        # 保持「不传即执行」的历史语义——见 HIGH_BLAST_TOOLS / _strict_confirm 注释）
        if _strict_confirm() and tname in HIGH_BLAST_TOOLS and args.get("confirm") is not True:
            return _input_required(mid,
                                   f"调用未执行：{tname} 属高破坏半径操作（不可逆/批量/跨项目）。"
                                   "严格确认模式下必须显式传 confirm=true 才执行；"
                                   "也可用 dry_run=true 先预演（return-and-resume，服务端不阻塞）。")
        # P3-1 dry_run：写操作预演（不落库、不审计）
        if tname in WRITE_TOOLS and args.get("dry_run") is True:
            preview = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items()
                                if k not in ("dry_run", "confirm", "max_chars"))
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": (
                        f"dry_run（未执行）：{tname} 预演通过，将写入 → {preview or '(无参数)'}\n"
                        "未落任何库；去掉 dry_run 后正式执行。")}], "isError": False}}
        _t0 = time.perf_counter()   # B8 埋点起点（AGENTHUB_LOG 未设时 _log_call 首行即返回，开销≈0）
        try:
            text = call_tool(tname, args, root)
            is_err = _is_error_text(text)
            if tname in WRITE_TOOLS and not is_err:
                _audit_write(root, tname, args, text)
            if is_err:
                # 服务端留痕（MCP 官方/ mcpcat 建议：内部记录完整错误，模型侧只给可行动文案，
                # 不把堆栈/SQL 直接塞回模型）。写 stderr，不污染 stdout 协议流。
                sys.stderr.write(f"[agenthub][tool-error] {tname} "
                                 f"agent={args.get('agent') or '-'} :: {text[:200]}\n")
                sys.stderr.flush()
            text, structured = _shape_result(text, args)
            result = {"content": [{"type": "text", "text": text}], "isError": is_err}
            if structured is not None:
                result["structuredContent"] = structured
            _log_call(root, tname, args, _t0, ok=not is_err, err_kind="tool_error" if is_err else "")
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as e:  # noqa: BLE001  工具错误以 isError 返回，不断连，并自动落库错误登记
            # 模型侧只给稳定可行动文案（含异常类型，不含原始堆栈/SQL/路径——MCP 官方 wati.io
            # 明确禁止把堆栈/SQL/秘密塞回模型）；完整细节只在 error_add + stderr 留痕（服务端可见）。
            # Round 11：区分「入参类型问题」与「服务端异常」——前者多因模型传了坏类型，
            # 文案要能自我纠正；此前一律渲染成「联系管理员」会把入参问题伪装成服务端故障，
            # 误导模型原地重试同一坏参数。登记时也分开归类（便于运维过滤，不淹没真故障）。
            input_err = isinstance(e, (TypeError, ValueError))
            if input_err:
                text = (f"错误：入参不符合要求（{type(e).__name__}）：请核对参数类型后重试"
                        "（如 limit / max_depth / record_id / since_id 等字段应为整数）")
            else:
                text = (f"工具执行失败（{type(e).__name__}）：已记入大脑错误，可重试；"
                        f"若持续失败联系管理员")
            try:
                brain.error_add(root, str(args.get("agent") or "mcp-server"),
                                f"MCP 工具 {tname} " + ("入参类型错误" if input_err else "执行异常"),
                                f"{type(e).__name__}: {e}",
                                project=str(args.get("project") or ""))
            except Exception:  # noqa: BLE001 登记失败不影响协议响应
                pass
            # B8：异常路径同样落痕——观测不到服务端异常的可观测性是假的（这类正是最该被看见的）
            _log_call(root, tname, args, _t0, ok=False,
                      err_kind="input_type" if input_err else "exception")
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
            try:
                # ensure_ascii=True：协议帧恒为纯 ASCII，不依赖 stdout 编码为 UTF-8，
                # 即使启动期 reconfigure 在异常环境下失败（被吞异常）也不会因中文抛 UnicodeEncodeError 炸进程。
                # 客户端 json.loads 会自动把 \uXXXX 还原为中文，行为无感。
                sys.stdout.write(json.dumps(resp, ensure_ascii=True) + "\n")
                sys.stdout.flush()
            except (OSError, UnicodeError) as e:
                log(f"写出失败（{type(e).__name__}）：{e}")
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
