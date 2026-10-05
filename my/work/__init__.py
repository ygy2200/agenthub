# -*- coding: utf-8 -*-
"""my.work —— AgentHub 工作史的可编程访问层（3.4 可编程个人数据包，HPI 模式）。

用法（把 AgentHub 源码目录或 MCP 部署目录加入 sys.path 后）：

    import sys; sys.path.insert(0, r"<AgentHub 目录>")
    import my.work as work

    work.records(since="5d")                       # 最近 5 天的记录
    work.records(since="2026-09", project="某-项目")
    work.memories(kind="lesson")                   # 全部踩坑记忆
    work.todos()                                   # 未勾销的待办线索
    work.profile()                                 # 用户画像（时段/分工/纠正史）
    work.stats()                                   # 规模统计

设计取舍（对照 karlicoss/HPI，详见项目 output/hpi学习心得.md）：
- 只读：全部函数只读 SQLite，不写入、不计入检索唤起统计——分析不污染数据
  （这也是不转调 brain.search_memories 的原因：它带 query 时会计 use_count）
- 零依赖：不做 namespace overlay / cachew / CLI 子命令——单机单库用不上
- 日期参数：ISO 日期（2026-09-15）、年月（2026-09，取当月 1 日）、
  相对时长 5d / 2w / 3mo（d 天、w 周、mo 月按 30 天计）
"""
from __future__ import annotations

import datetime
import re

import brain
import core

__all__ = ["records", "memories", "todos", "profile", "projects", "stats"]


def _resolve_root(root: str = "") -> str:
    r = root or core.get_root()
    if not r:
        raise RuntimeError(
            "根目录未设置：core.get_root() 为空——先运行 AgentHub 设置根目录，或显式传 root=参数")
    return r


def _parse_date(s) -> str:
    """'2026-09'→当月 1 日；'2026-09-15'→原样；'5d'/'2w'/'3mo'→相对今天；空值→''。

    非法值（坏格式/月>12/不存在的日期/超 100 年的相对时长）一律 ValueError 清晰拒绝，
    绝不静默返回可疑日期——否则 SQL 字符串比较会产生错误范围。"""
    if s is None:
        return ""
    s = str(s).strip()
    if not s:
        return ""
    m = re.fullmatch(r"(\d+)(d|w|mo)", s, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        if n > 36500:  # 100 年：防 timedelta 溢出，也拦下无意义的巨值
            raise ValueError(f"相对时长超出 100 年：{s!r}")
        days = {"d": 1, "w": 7, "mo": 30}[m.group(2).lower()] * n
        return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}", s):
        if not 1 <= int(s[5:7]) <= 12:
            raise ValueError(f"月份不合法：{s!r}")
        return f"{s}-01"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            datetime.date.fromisoformat(s)
        except ValueError:
            raise ValueError(f"日期不存在：{s!r}") from None
        return s
    raise ValueError(f"无法解析日期参数 {s!r}——支持 ISO 日期(2026-09-15)、年月(2026-09)、"
                     "相对时长(5d/2w/3mo)")


def records(since=None, until=None, project: str = "", agent: str = "",
            limit: int = 500, root: str = "") -> list:
    """工作记录（只读）。since/until 支持 ISO/年月/相对时长；日期倒序。"""
    rs = _resolve_root(root)
    q = "SELECT * FROM records WHERE status='active'"
    args: list = []
    d1 = _parse_date(since)
    if d1:
        q += " AND date>=?"
        args.append(d1)
    d2 = _parse_date(until)
    if d2:
        q += " AND date<=?"
        args.append(d2)
    if project:
        q += " AND project=?"
        args.append(str(project))
    if agent:
        q += " AND agent=?"
        args.append(str(agent))
    q += " ORDER BY date DESC, id DESC LIMIT ?"
    args.append(max(0, min(int(limit), 5000)))
    with brain.db_conn(rs) as conn:
        return [dict(r) for r in conn.execute(q, args)]


def memories(kind: str = "", tag: str = "", query: str = "",
             limit: int = 100, root: str = "") -> list:
    """记忆检索（只读，不计入唤起统计）。tag/query 为 SQL LIKE 包含式匹配
    （% _ 是通配符，非转义字面量）。"""
    rs = _resolve_root(root)
    q = "SELECT * FROM memories WHERE status='active'"
    args: list = []
    if kind:
        q += " AND kind=?"
        args.append(str(kind))
    if tag:
        q += " AND tags LIKE ?"
        args.append(f"%{tag}%")
    if query:
        q += " AND content LIKE ?"
        args.append(f"%{query}%")
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(0, min(int(limit), 2000)))
    with brain.db_conn(rs) as conn:
        return [dict(r) for r in conn.execute(q, args)]


def todos(limit: int = 20, root: str = "") -> list:
    """未勾销的待办线索（转调 brain.extract_todos）。"""
    return brain.extract_todos(_resolve_root(root), limit=limit)


def profile(root: str = "") -> dict:
    """用户画像：活跃时段/记录分工/技术栈词频/纠正史 top（brain.user_profile）。"""
    return brain.user_profile(_resolve_root(root))


def projects(limit: int = 50, root: str = "") -> list:
    """项目清单（brain.list_projects）。"""
    return brain.list_projects(_resolve_root(root), limit=limit)


def stats(root: str = "") -> dict:
    """规模统计（brain.stats）。"""
    return brain.stats(_resolve_root(root))
