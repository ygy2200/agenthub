# -*- coding: utf-8 -*-
"""反馈报告（v2.11 阶段 1.3）：把大脑数据变成"带修复建议的反馈"，不是流水账汇总。

依据 02-落地计划 1.3（mentor 式反馈回路，"往外倒"是续命的关键一环）：
- 产出形态硬性要求：单个自包含 HTML——内联 CSS/SVG、无外链、无构建、双击即开
- 每处摩擦必须给一个可复用的修复——一条规则/一个习惯/一个工具调用，
  让同样的错下周不再出现（mentor 的设计承诺）
- 零 LLM 完整闭环：全部规则拼装；模板按章节函数组装，增删章节改 SECTIONS 即可
- 铁律 9：「建议的规则/技能修改」一律标注为提案，经人确认才写入，绝不自动改

入口：generate_report(root) → 返回 HTML 路径（MCP 工具 hub_report 暴露）。
"""
from __future__ import annotations

import datetime
import html
from pathlib import Path

import brain

# 展示层截断：超长列表/内容按 top-N 裁剪，报告体积与可读性都受控
TOP_PROJECTS = 10
TOP_MEMORIES = 8
MAX_LIST = 8


def _esc(s) -> str:
    """所有动态内容进 HTML 前必须过这里（对抗：项目名/记忆内容含 <script> 等）。"""
    return html.escape(str(s if s is not None else ""), quote=True)


# ---------------------------------------------------------------- 数据采集（只读）

def _collect(root: str) -> dict:
    h = brain.health_report(root)
    d = {"h": h, "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    with brain.db_conn(root) as conn:
        d["proj_top"] = [dict(r) for r in conn.execute(
            "SELECT project, COUNT(*) n, COUNT(DISTINCT date) days, MIN(date) d1, MAX(date) d2 "
            "FROM records WHERE status='active' GROUP BY project ORDER BY n DESC LIMIT ?",
            (TOP_PROJECTS,))]
        row = conn.execute(
            "SELECT COUNT(*) FROM (SELECT project FROM records WHERE status='active' "
            "GROUP BY project HAVING COUNT(DISTINCT date)=1)").fetchone()
        d["proj_total_active"] = conn.execute(
            "SELECT COUNT(DISTINCT project) FROM records WHERE status='active'").fetchone()[0]
        d["oneday_projects"] = row[0]
        d["monthly"] = [dict(r) for r in conn.execute(
            "SELECT substr(date,1,7) m, COUNT(*) n FROM records WHERE status='active' AND date!='' "
            "GROUP BY m ORDER BY m DESC LIMIT 12")]
        d["agents"] = {r["agent"] or "其他": r["n"] for r in conn.execute(
            "SELECT agent, COUNT(*) n FROM records WHERE status='active' GROUP BY agent ORDER BY n DESC LIMIT 8")}
        d["done_todos"] = conn.execute("SELECT COUNT(*) FROM todos_done").fetchone()[0]
        d["zero_mem"] = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active' AND use_count=0").fetchone()[0]
        d["tool_used"] = {r[0]: r[1] for r in conn.execute(
            "SELECT tool, COUNT(*) n FROM searches GROUP BY tool")}
    return d


# ---------------------------------------------------------------- 章节 builders
# 每节返回 HTML 片段；增删章节改 SECTIONS 列表即可（模板可定制的实现方式）

def _sec_doing(d) -> str:
    top, total = d["proj_top"], d["h"]["records"]
    rows = "".join(
        f"<tr><td>{_esc(p['project'])}</td><td>{p['n']}</td>"
        f"<td>{round(p['n'] * 100 / max(1, total), 1)}%</td>"
        f"<td>{_esc(p['d1'] or '?')} ~ {_esc(p['d2'] or '?')}</td></tr>"
        for p in top)
    return (f"<p>共 <b>{total}</b> 条记录、<b>{d['h']['memories']}</b> 条记忆、"
            f"<b>{d['h']['projects'] - d['h']['projects_archived']}</b> 个活跃项目。"
            f"记录量前十的项目：</p>"
            f"<table><tr><th>项目</th><th>记录</th><th>占比</th><th>跨度</th></tr>{rows}</table>")


def _sec_time(d) -> str:
    oneday_pct = round(d["oneday_projects"] * 100 / max(1, d["proj_total_active"]), 1)
    bars = _bar_chart(d["monthly"])
    return (f"<p>「只活一天」的项目（仅一天有记录）占 <b>{oneday_pct}%</b>"
            f"（{d['oneday_projects']}/{d['proj_total_active']}）——"
            f"一次性事务为主是常态，但高价值项目值得长期续写。</p>"
            f"<h3>近 12 个月记录量</h3>{bars}")


def _bar_chart(monthly: list) -> str:
    """内联 SVG 柱状图（无 JS 无外链）。monthly 新在前，展示时翻转为时间轴正序。"""
    if not monthly:
        return "<p class='muted'>（暂无带日期的记录）</p>"
    data = list(reversed(monthly))[-12:]
    mx = max(r["n"] for r in data) or 1
    bw, gap, hmax = 42, 10, 120
    parts = [f"<svg width='{len(data) * (bw + gap) + 20}' height='{hmax + 40}' role='img'>"]
    for i, r in enumerate(data):
        bh = max(4, round(r["n"] * hmax / mx))
        x = 10 + i * (bw + gap)
        parts.append(f"<rect x='{x}' y='{hmax + 10 - bh}' width='{bw}' height='{bh}' rx='3' class='bar'/>")
        parts.append(f"<text x='{x + bw // 2}' y='{hmax + 6 - bh}' text-anchor='middle' class='v'>{r['n']}</text>")
        parts.append(f"<text x='{x + bw // 2}' y='{hmax + 28}' text-anchor='middle' class='m'>{_esc(r['m'][2:])}</text>")
    parts.append("</svg>")
    return "".join(parts)


def _sec_agents(d) -> str:
    ag = "、".join(f"{_esc(k)} {v}条" for k, v in d["agents"].items())
    tools = d["h"]["tool_breakdown"]
    tool_line = "、".join(f"{_esc(k)} {v}" for k, v in list(tools.items())[:8]) or "无"
    push = d["h"].get("push_by_agent", {})
    push_line = "、".join(f"{_esc(k)} {v}次" for k, v in push.items()) or "无"
    srch = d["h"].get("search_by_agent", {})
    srch_line = "、".join(f"{_esc(k)} {v}次" for k, v in srch.items()) or "无"
    return (f"<p><b>记录量：</b>{ag}</p>"
            f"<p><b>检索工具配比：</b>{tool_line}</p>"
            f"<p><b>被动推送（开工简报）：</b>{push_line}　<b>主动检索：</b>{srch_line}</p>"
            f"<p class='muted'>读得勤的 agent 成长快——主动检索是最高价值的唤起信号。</p>")


def _sec_wins(d) -> str:
    h = d["h"]
    top = h.get("top_pushed", [])[:TOP_MEMORIES]
    lis = "".join(
        f"<li>#{m['id']}（唤起 {_esc(m['use_count'])} 次）[{_esc(brain.KIND_CN.get(m['kind'], m['kind']))}] "
        f"{_esc((m['content'] or '')[:70])}</li>"
        for m in top) or "<li>（还没有被唤起过的记忆——先用心跳与检索养起来）</li>"
    closed = d["done_todos"]
    honest = h.get("honest", {})
    return (f"<h3>被唤起最多的记忆（真实帮到工作的知识）</h3><ul>{lis}</ul>"
            f"<p><b>已闭环的待办 {closed} 条</b>（承诺过的事被勾销）· "
            f"记忆有用率 <b>{honest.get('memory_use_rate', 0)}%</b> · "
            f"结晶率 <b>{h['crystallization']}%</b></p>")


def _frictions(d) -> list:
    """摩擦点：每条 {title, evidence, fix}。数据驱动检测——修复必须可执行。"""
    h, out = d["h"], []
    if h["errors_open"]:
        out.append({"title": f"{h['errors_open']} 条错误登记未处置",
                    "evidence": "open 状态的错误登记挂在账上，下次踩坑时拦截提醒会反复弹它们",
                    "fix": 'hub_list_errors 逐条看：已修复的 hub_report_error 无需重复，确认处置后走错误状态流转'})
    if h["dup_memory_count"]:
        out.append({"title": f"{h['dup_memory_count']} 组疑似重复记忆",
                    "evidence": "Jaccard 相似度检出，重复记忆让检索结果互相稀释",
                    "fix": "按「取代记忆#N」约定：写一条修正版并标注取代谁，全员裁决旧条目"})
    meta_ratio = h.get("honest", {}).get("meta_ratio", 0)
    if meta_ratio >= 40:
        out.append({"title": f"元信息占比 {meta_ratio}%（记忆在说 AgentHub 自己，不是你的工作）",
                    "evidence": f"use_count 从未参与的元记忆挤占了 {h['memories']} 条记忆的库容",
                    "fix": "蒸馏时跳过 [元信息?] 候选；新记忆写工作知识（坑、事实、决策），系统元信息不进记忆"})
    zero = d["zero_mem"]
    if h["memories"] and zero * 2 >= h["memories"]:
        out.append({"title": f"{zero}/{h['memories']} 条记忆从未被唤起",
                    "evidence": "零唤起记忆在推送排序里已优先轮换曝光（v2.8.4），但仍大面积沉默说明内容本身低价值",
                    "fix": "hub_health 看保鲜清单：环境已漂移的更新，无效的归档——用进废退"})
    if h["projects_stalled"]:
        out.append({"title": f"{h['projects_stalled']} 个项目停滞（90 天无活动）",
                    "evidence": "停滞项目挤占列表与注意力，真正活跃的事被淹没",
                    "fix": "hub_archive_project 归档（可逆：状态改回 active 即恢复），需要用户拍板"})
    return out


def _sec_friction(d) -> str:
    frs = _frictions(d)
    if not frs:
        return "<p>没有检出摩擦点——数据干净，保持节奏。</p>"
    blocks = "".join(
        f"<div class='fric'><b>⚠ {_esc(f['title'])}</b><br>"
        f"<span class='ev'>证据：{_esc(f['evidence'])}</span><br>"
        f"<span class='fix'>修复：{_esc(f['fix'])}</span></div>"
        for f in frs)
    return blocks + "<p class='muted'>每一处摩擦都配了一个可复用的修复——让同样的错下周不再出现。</p>"


# 检查工具箱里的功能清单：与 searches 日志对比找"装了没用过"的能力
_KNOWN_TOOLS = {
    "hub_ops_run": "检查工具箱（py_compile/回归/部署一致性一条命令）",
    "hub_env_set": "环境档案（本机配置结构化登记，agent 开工即读）",
    "hub_todo_done": "待办勾销（欠账闭环）",
    "hub_distill": "蒸馏候选（记录→记忆的结晶流水线）",
    "hub_duplicates": "数据完整性检测（重复目录/逐字重复）",
    "hub_list_skills": "技能库对齐（看别的 agent 会什么）",
    "hub_report": "本反馈报告",
    "hub_get_record": "记录精读（渐进披露 full/abstract/outline）",
}


def _sec_try(d) -> str:
    used = set(d["tool_used"])
    unused = [(t, desc) for t, desc in _KNOWN_TOOLS.items()
              if t not in used and t != "hub_report"]
    if not unused:
        return "<p>工具箱里的能力都用起来了，没有闲置。</p>"
    lis = "".join(f"<li><code>{_esc(t)}</code>：{_esc(desc)}</li>" for t, desc in unused[:MAX_LIST])
    return (f"<p>以下能力已内置但从未使用（检索日志零记录）：</p><ul>{lis}</ul>")


def _sec_rules(d) -> str:
    """建议的规则/技能修改——可复制粘贴的提案行。⚠ 铁律 9：提案，人确认才入库。"""
    props = []
    if d["h"].get("honest", {}).get("meta_ratio", 0) >= 40:
        props.append("写入记忆时 kind 必须选对（坑=lesson、事实=fact、偏好=preference），"
                     "系统自身元信息禁止 hub_memory_write——直接在记录里说清即可")
    if d["done_todos"] == 0:
        props.append("答应过的事（待办/后续/下次…）完成后必须 hub_todo_done 勾销，欠账不过夜")
    if d["h"]["errors_open"]:
        props.append("踩坑登记后 48h 内必须复核一次状态：确认修复的错误要及时闭环，别让拦截提醒反复弹旧账")
    if not props:
        props.append("当前数据没有指向明确的规则缺口——保持现有约定，下次复盘再看")
    lis = "".join(f"<li><code>{_esc(p)}</code></li>" for p in props)
    return ("<ul>" + lis + "</ul>"
            "<p class='warn'>⚠ 以上是提案，采纳后请人工写入 AGENTS.md/技能库——系统不自动改规则（铁律 9）。</p>")


def _sec_better(d) -> str:
    tips = []
    if d["h"]["searches_total"] < 50:
        tips.append("检索量偏低：开工时 hub_memory_read 带 query 查相关记忆、收工前把新知识沉淀，"
                    "读写的飞轮转起来大脑才有用")
    if not d["h"].get("search_by_agent"):
        tips.append("还没有 agent 主动检索过记忆——让常用 agent 的引导块里加一句「开工先 hub_memory_read 查本项目踩坑」")
    tips.append("长任务开工先 hub_heartbeat（撞车预警+项目记忆自动推送），收工 hub_log_work（含收尾建议）")
    lis = "".join(f"<li>{_esc(t)}</li>" for t in tips[:5])
    return f"<ul>{lis}</ul>"


def _sec_horizon(d) -> str:
    h = d["h"]
    stalled = h["projects_stalled"]
    arch = h["projects_archived"]
    stale = h.get("stale_memories", [])
    line = (f"<p>活跃项目 <b>{h['projects'] - arch}</b> · 停滞 <b>{stalled}</b> · 已归档 <b>{arch}</b>"
            f" · 保鲜提醒（90 天未唤起的 lesson/fact）<b>{len(stale)}</b> 条</p>")
    if stale:
        lis = "".join(f"<li>#{m['id']} {_esc(m['content'][:56])}</li>" for m in stale[:5])
        line += f"<ul>{lis}</ul>"
    return line + ("<p class='muted'>归档不是删除：hub_archive_project 可逆，"
                   "记忆保鲜清单里的条目确认仍有效或更新后即可移出。</p>")


# 章节注册表：顺序即报告顺序；增删章节改这里（模板可定制的落点）
SECTIONS = [
    ("你在做什么", _sec_doing),
    ("你的时间花在哪类活上", _sec_time),
    ("你怎么用 agent", _sec_agents),
    ("你做对了什么", _sec_wins),
    ("哪里出了问题", _sec_friction),
    ("值得一试的能力", _sec_try),
    ("建议的规则/技能修改（提案）", _sec_rules),
    ("更好的用法", _sec_better),
    ("在视野之内", _sec_horizon),
]

_CSS = """
body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;max-width:880px;margin:24px auto;
     padding:0 16px;color:#1c2733;line-height:1.65;background:#fafbfc}
h1{font-size:1.5em;border-bottom:3px solid #0a7ea4;padding-bottom:8px}
h2{font-size:1.15em;color:#0a7ea4;margin-top:34px;border-left:4px solid #0a7ea4;padding-left:10px}
h3{font-size:1em;color:#37506a}
table{border-collapse:collapse;margin:8px 0;font-size:.92em}
th,td{border:1px solid #d5dde5;padding:4px 12px;text-align:left}
th{background:#eef3f7}
code{background:#eef1f4;padding:1px 6px;border-radius:4px;font-size:.9em}
.fric{border:1px solid #e8d8b8;background:#fdf8ee;border-radius:8px;padding:10px 14px;margin:10px 0}
.fric .ev{color:#7a6a45;font-size:.92em}
.fric .fix{color:#0a6b3d;font-size:.92em}
.muted{color:#68788a;font-size:.92em}
.warn{color:#a05a00;font-size:.92em}
.bar{fill:#0a7ea4;opacity:.85}
.v{font-size:11px;fill:#37506a}
.m{font-size:10px;fill:#68788a}
.meta{color:#68788a;font-size:.9em}
nav{font-size:.9em;margin:12px 0}nav a{color:#0a7ea4;margin-right:12px}
"""


def _html_doc(d: dict, secs: list) -> str:
    """报告骨架。secs: [(标题, html)]——全部动态内容已在 builder 内转义。"""
    h = d["h"]
    honest = h.get("honest", {})
    nav = "".join(f"<a href='#sec{i}'>{_esc(t)}</a>" for i, (t, _) in enumerate(secs))
    body = "".join(
        f"<h2 id='sec{i}'>{_esc(t)}</h2>{fn_html}"
        for i, (t, fn_html) in enumerate(secs))
    return (f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            f"<title>AgentHub 反馈报告 {d['generated'][:10]}</title><style>{_CSS}</style></head><body>"
            f"<h1>大脑反馈报告 · {d['generated']}</h1>"
            f"<p class='meta'>记录 {h['records']} · 记忆 {h['memories']} · "
            f"检索累计 {h['searches_total']} · 记忆有用率 {honest.get('memory_use_rate', 0)}% · "
            f"元信息占比 {honest.get('meta_ratio', 0)}%</p>"
            f"<p class='meta'>口径：{_esc(honest.get('note', ''))}</p>"
            f"<nav>{nav}</nav>{body}"
            f"<p class='muted'>本报告由规则拼装（零 LLM），摩擦与建议均为提案——采纳由人拍板。"
            f"重新生成：hub_report。</p></body></html>")


def generate_report(root: str, out: str = "") -> str:
    """生成单文件 HTML 反馈报告，返回文件路径。默认落 <root>/_hub/reports/（同日覆盖）。"""
    d = _collect(root)
    secs = [(title, fn(d)) for title, fn in SECTIONS]
    doc = _html_doc(d, secs)
    p = Path(out) if out else Path(root) / "_hub" / "reports" / f"反馈报告-{datetime.date.today().isoformat()}.html"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(doc, encoding="utf-8")
    return str(p)
