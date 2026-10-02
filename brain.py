# -*- coding: utf-8 -*-
"""AgentHub 大脑：SQLite 结构化存储层（记忆/工作记录/流水/错误/会话/文件索引）。

设计：
- brain.db 位于 <root>/_hub/brain.db，WAL 模式，GUI 与各 agent 的 MCP server 多进程并发安全
- 历史 md/jsonl/json 增量迁移入库（幂等可重复跑），原文件保留为只读存档；此后 DB 是唯一真理
- 记忆五类 kind：fact 环境事实 / preference 用户偏好 / lesson 踩坑经验 / project 项目进展 / note 随手记
- 本模块 import core（复用记录解析），core 不反向 import 本模块（core 的流水埋点走 JOURNAL_SINK 注入）
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import core

DB_NAME = "brain.db"
BACKUP_DIR = "brain-backups"
BACKUP_KEEP = 30
KINDS = ("fact", "preference", "lesson", "project", "note")
KIND_CN = {"fact": "环境事实", "preference": "用户偏好", "lesson": "踩坑经验",
           "project": "项目进展", "note": "随手记"}
SESSION_TTL = 1800     # 会话心跳陈旧阈值（秒）
SESSION_ACTIVE = 300   # 同项目"正在工作"判定窗口（秒）
STALL_DAYS = 90        # 项目停滞判定：超过该天数无记录即 stalled
MAX_CONTENT = 128 * 1024


def db_path(root: str) -> Path:
    return Path(root) / core.DIR_META / DB_NAME


@contextmanager
def db_conn(root: str):
    """打开一个短连接（WAL + busy_timeout），用完即关。多进程并发安全。
    不主动创建目录/文件——建库责任在 init_db，无效 root 直接抛错。"""
    p = db_path(root)
    conn = sqlite3.connect(str(p), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    agent TEXT DEFAULT "",
    note TEXT DEFAULT "",
    created TEXT DEFAULT "",
    status TEXT DEFAULT "active",
    updated TEXT DEFAULT ""
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    agent TEXT DEFAULT "",
    date TEXT DEFAULT "",
    title TEXT DEFAULT "",
    content TEXT DEFAULT "",
    source TEXT DEFAULT "hub",
    status TEXT DEFAULT "active",
    created TEXT DEFAULT ""
);
CREATE INDEX IF NOT EXISTS idx_records_proj ON records(project);
CREATE INDEX IF NOT EXISTS idx_records_date ON records(date);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT DEFAULT "note",
    content TEXT NOT NULL,
    tags TEXT DEFAULT "",
    project TEXT DEFAULT "",
    agent TEXT DEFAULT "",
    pinned INTEGER DEFAULT 0,
    status TEXT DEFAULT "active",
    created TEXT DEFAULT "",
    updated TEXT DEFAULT "",
    use_count INTEGER DEFAULT 0,
    last_hit TEXT DEFAULT ""
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, agent TEXT, action TEXT, target TEXT, backup TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, agent TEXT, project TEXT, title TEXT, detail TEXT,
    undo TEXT, status TEXT DEFAULT "open"
);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, agent TEXT DEFAULT "", tool TEXT DEFAULT "",
    query TEXT DEFAULT "", hits INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    agent TEXT PRIMARY KEY,
    project TEXT DEFAULT "", note TEXT DEFAULT "", ts TEXT DEFAULT ""
);
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    home TEXT DEFAULT "",
    kind TEXT DEFAULT "auto",
    note TEXT DEFAULT "",
    first_seen TEXT DEFAULT "",
    last_seen TEXT DEFAULT "",
    last_project TEXT DEFAULT "",
    heartbeats INTEGER DEFAULT 0,
    records INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS env_items (
    category TEXT,
    key TEXT,
    value TEXT,
    agent TEXT DEFAULT "",
    updated TEXT,
    PRIMARY KEY (category, key)
);
CREATE TABLE IF NOT EXISTS todos_done (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER,
    todo TEXT,
    agent TEXT DEFAULT "",
    ts TEXT
);
CREATE TABLE IF NOT EXISTS files (
    project TEXT, path TEXT, name TEXT, grp TEXT,
    size INTEGER DEFAULT 0, mtime REAL DEFAULT 0,
    PRIMARY KEY (project, path)
);
"""


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# user=GUI 本机操作、unknown/migrated=历史数据来源，不算 agent 身份
_NON_AGENT = {"", "user", "unknown", "migrated"}


def _norm_agent(agent: str) -> str:
    """agent 身份统一小写：ZCode/zcode 双身份曾并存，过滤/统计/徽章处处错位
    （2026-10-01 时间线过滤滤光记录事故根因）。"""
    return (agent or "").strip().lower()


def _upsert_agent(conn, agent: str, project: str = "", beat: bool = False, record: bool = False) -> None:
    """agent 注册制：所有写动作顺带登记身份（轻量 upsert，见 agents 表）。"""
    agent = _norm_agent(agent)
    if agent in _NON_AGENT:
        return
    now = _now()
    conn.execute("INSERT INTO agents(name,first_seen,last_seen,last_project) VALUES(?,?,?,?) "
                 "ON CONFLICT(name) DO NOTHING", (agent, now, now, project or ""))
    sets, args = ["last_seen=?"], [now]
    if project:
        sets.append("last_project=?")
        args.append(project)
    if beat:
        sets.append("heartbeats=heartbeats+1")
    if record:
        sets.append("records=records+1")
    args.append(agent)
    conn.execute(f"UPDATE agents SET {','.join(sets)} WHERE name=?", args)


def init_db(root: str) -> str:
    """建表 + 增量迁移（幂等，每次启动跑，已有数据不重复导入）。返回错误或 ""。"""
    if not root or not os.path.isdir(root):
        return "根目录不存在"
    try:
        db_path(root).parent.mkdir(parents=True, exist_ok=True)  # 建库责任的唯一入口
        with db_conn(root) as conn:
            conn.executescript(SCHEMA)
        _migrate_records(root)
        _migrate_memory(root)
        _migrate_jsonl(root)
        _backfill_agents(root)
        _normalize_agents(root)
        _migrate_columns(root)
        return ""
    except sqlite3.Error as e:
        return f"大脑数据库初始化失败：{e}"


# ---------------------------------------------------------------- 迁移（幂等增量）

def _migrate_records(root: str) -> int:
    """扫描全部项目目录导入记录；所有非保留目录登记进 projects。返回本次导入条数。

    对齐 core.scan 的记录提取语义：
    - 有 工作记录.md 用之；否则用目录下最新 md/txt 顶上，整篇无段头按 "[主文档]" 一条
    - 段头/段内解析不出日期时用文档 mtime 兜底（fallback_date）
    幂等：先清掉 date 为空的旧迁移记录（早期版本未传 fallback_date 的缺日期数据），
    再按 (project,date,agent,title,content) 去重增量导入，可安全重复跑。
    去重键含 content：同一项目里存在段头完全相同的多条段（如同名 "## 日期（agent）"），
    只按段头去重会吞掉第二条的内容。"""
    n = 0
    with db_conn(root) as conn:
        conn.execute("DELETE FROM records WHERE source LIKE 'migrated:%' AND date=''")
        known = {(r["project"], r["date"], r["agent"], r["title"], (r["content"] or "")[:256])
                 for r in conn.execute("SELECT project,date,agent,title,content FROM records")}
        for entry in os.scandir(root):
            if not entry.is_dir() or entry.name in core.RESERVED:
                continue
            conn.execute("INSERT OR IGNORE INTO projects(name, created) VALUES(?,?)",
                         (entry.name, _now()))
            p = Path(entry.path)
            src = p / core.RECORD_NAME
            if not src.is_file():
                try:
                    cands = [f for f in p.iterdir()
                             if f.is_file() and f.suffix.lower() in core.SEARCH_EXTS]
                except OSError:
                    cands = []
                if not cands:
                    continue
                src = max(cands, key=lambda f: f.stat().st_mtime)
            pm = core.AGENT_PREFIX_RE.match(entry.name)
            fallback = pm.group(1).lower() if pm else "zcode"
            entries = core.parse_record(core.read_text(src), fallback_agent=fallback,
                                        fallback_date=core._mtime_date(src))
            if not entries and src.name != core.RECORD_NAME:  # 主文档整篇无段头 → 文件级一条
                entries = [core.RecordEntry(date=core._mtime_date(src), agent=fallback,
                                            agent_raw="", title=f"[主文档] {src.name}", line_no=0,
                                            body=core.read_text(src)[:4000])]
            for r in entries:
                key = (entry.name, r.date, r.agent, r.title, (r.body or "")[:256])
                if key in known:
                    continue
                conn.execute(
                    "INSERT INTO records(project,agent,date,title,content,source,created) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (entry.name, r.agent, r.date, r.title, r.body,
                     f"migrated:{src.name}:{r.line_no}", _now()))
                known.add(key)
                n += 1
    return n


def _migrate_memory(root: str) -> int:
    """memory.md 自由文本逐行导入 memories(kind=note)，已导入的跳过。"""
    f = core.memory_file(root)
    if not f.is_file():
        return 0
    n = 0
    with db_conn(root) as conn:
        known = {r["content"] for r in conn.execute("SELECT content FROM memories")}
        for line in core.read_text(f).splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s not in known:
                conn.execute(
                    "INSERT INTO memories(kind,content,agent,created,updated) VALUES('note',?,?,?,?)",
                    (s, "migrated", _now(), _now()))
                known.add(s)
                n += 1
    return n


def _migrate_jsonl(root: str) -> int:
    """journal.jsonl / errors.jsonl / sessions.json 一次性导入（meta 标记防重跑）。"""
    n = 0
    meta = _meta_get(root, "migrated_jsonl")
    if meta == "done":
        return 0
    with db_conn(root) as conn:
        jf = core.journal_file(root) if hasattr(core, "journal_file") else Path(root) / core.DIR_META / "journal.jsonl"
        if jf.is_file():
            for line in core.read_text(jf).splitlines():
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                conn.execute("INSERT INTO journal(ts,agent,action,target,backup,note) VALUES(?,?,?,?,?,?)",
                             (d.get("ts", ""), d.get("agent", ""), d.get("action", ""),
                              d.get("target", ""), d.get("backup", ""), d.get("note", "")))
                n += 1
        ef = Path(root) / core.DIR_META / "errors.jsonl"
        if ef.is_file():
            for line in core.read_text(ef).splitlines():
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                conn.execute("INSERT INTO errors(id,ts,agent,project,title,detail,undo,status) "
                             "VALUES(?,?,?,?,?,?,?,?)",
                             (d.get("id"), d.get("ts", ""), d.get("agent", ""), d.get("project", ""),
                              d.get("title", ""), d.get("detail", ""), d.get("undo", ""),
                              d.get("status", "open")))
                n += 1
        sf = Path(root) / core.DIR_META / "sessions.json"
        if sf.is_file():
            try:
                for s in json.loads(core.read_text(sf) or "[]"):
                    if isinstance(s, dict) and s.get("agent"):
                        conn.execute("INSERT OR REPLACE INTO sessions(agent,project,note,ts) VALUES(?,?,?,?)",
                                     (s["agent"], s.get("project", ""), s.get("note", ""), s.get("ts", "")))
                        n += 1
            except Exception:  # noqa: BLE001
                pass
        _meta_set_conn(conn, "migrated_jsonl", "done")
    return n


# ---------------------------------------------------------------- meta

def _meta_set_conn(conn, key: str, value: str):
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))


def meta_set(root: str, key: str, value: str):
    with db_conn(root) as conn:
        _meta_set_conn(conn, key, value)


def _meta_get(root: str, key: str) -> str:
    with db_conn(root) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""


def _backfill_agents(root: str) -> None:
    """历史 records 的 agent 回填注册表（meta 标记防重跑，幂等）。"""
    with db_conn(root) as conn:
        if conn.execute("SELECT value FROM meta WHERE key='agents_backfill'").fetchone():
            return
        for r in conn.execute(
                "SELECT agent, COUNT(*) AS n, MIN(date) AS d1, MIN(created) AS d2 "
                "FROM records WHERE agent NOT IN ('','user','unknown','migrated') GROUP BY agent"):
            first = r["d1"] or r["d2"] or ""
            conn.execute("INSERT OR IGNORE INTO agents(name,first_seen,records) VALUES(?,?,?)",
                         (r["agent"], first, r["n"]))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('agents_backfill','1')")


def _normalize_agents(root: str) -> None:
    """历史数据 agent 大小写归一（ZCode/zcode 双身份清理，幂等）。
    agents 表主键冲突行合并：计数累加、首见/最近取极值。"""
    with db_conn(root) as conn:
        for tbl in ("records", "memories", "errors", "journal"):
            conn.execute(f"UPDATE {tbl} SET agent=lower(agent) "
                         "WHERE agent IS NOT NULL AND agent!=lower(agent)")
        for r in [dict(x) for x in conn.execute("SELECT * FROM agents")]:
            low = (r["name"] or "").strip().lower()
            if not low or low == r["name"]:
                continue
            dup = conn.execute("SELECT name FROM agents WHERE name=?", (low,)).fetchone()
            if dup:
                conn.execute("UPDATE agents SET records=records+?, heartbeats=heartbeats+?, "
                             "first_seen=MIN(first_seen,?), last_seen=MAX(last_seen,?) WHERE name=?",
                             (r["records"] or 0, r["heartbeats"] or 0,
                              r["first_seen"] or "", r["last_seen"] or "", low))
                conn.execute("DELETE FROM agents WHERE name=?", (r["name"],))
            else:
                conn.execute("UPDATE agents SET name=? WHERE name=?", (low, r["name"]))


def _migrate_columns(root: str) -> None:
    """老库增量补列（幂等）：memories 用进废退、projects 生命周期；projects.updated 按 records 回填，
    超过 STALL_DAYS 无活动标 stalled。"""
    with db_conn(root) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
        if "use_count" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN use_count INTEGER DEFAULT 0")
        if "last_hit" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN last_hit TEXT DEFAULT ''")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
        if "status" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN status TEXT DEFAULT 'active'")
        if "updated" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN updated TEXT DEFAULT ''")
        # ALTER 后刷新列集合，保证首次迁移即执行回填
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
        if "updated" in cols:
            conn.execute(
                "UPDATE projects SET updated=(SELECT MAX(r.date) FROM records r "
                "WHERE r.project=projects.name AND r.status='active') "
                "WHERE updated='' OR updated IS NULL")
            conn.execute(
                "UPDATE projects SET status='stalled' WHERE status='active' AND updated!='' "
                "AND julianday('now')-julianday(updated) > ?", (STALL_DAYS,))


# ---------------------------------------------------------------- 工作记录

def add_record(root: str, project: str, agent: str, date: str, title: str, content: str) -> int:
    """写一条工作记录（hub 入口）。自动登记项目（历史目录迁移时已登记），并刷新项目活跃状态。"""
    with db_conn(root) as conn:
        conn.execute("INSERT OR IGNORE INTO projects(name, created) VALUES(?,?)", (project, _now()))
        agent = _norm_agent(agent)
        _upsert_agent(conn, agent, project, record=True)
        cur = conn.execute(
            "INSERT INTO records(project,agent,date,title,content,source,created) VALUES(?,?,?,?,?,'hub',?)",
            (project, agent, date, title, content, _now()))
        conn.execute("UPDATE projects SET updated=?, status='active' "
                     "WHERE name=? AND status!='archived'", (date or _now()[:10], project))
        return cur.lastrowid


def undo_last_record(root: str, agent: str) -> tuple:
    """软删该 agent 最后一条 hub 写入的记录（栈式）。返回 (错误或"", 撤掉的记录摘要)。"""
    agent = _norm_agent(agent)
    with db_conn(root) as conn:
        row = conn.execute(
            "SELECT id,project,date,title FROM records WHERE agent=? AND source='hub' AND status='active' "
            "ORDER BY id DESC LIMIT 1", (agent,)).fetchone()
        if not row:
            return f"没有找到 {agent} 可撤销的 hub 记录", ""
        conn.execute("UPDATE records SET status='deleted' WHERE id=?", (row["id"],))
        return "", f"#{row['id']} {row['date']} {row['project']}：{row['title']}"


def list_records(root: str, project: str = "", agent: str = "", limit: int = 100) -> list:
    q = "SELECT * FROM records WHERE status='active'"
    args: list = []
    if project:
        q += " AND project=?"
        args.append(project)
    if agent:
        q += " AND agent=?"
        args.append(agent)
    q += " ORDER BY date DESC, id DESC LIMIT ?"
    args.append(max(0, min(int(limit), 2000)))
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(q, args)]


def search_records(root: str, kw: str, limit: int = 30) -> list:
    """多词评分检索：query 按空白切词，命中词数越多越靠前（同分按日期）。
    单词行为兼容旧版；评分让"多关键词"查询真正缩小范围而非取并集噪声。"""
    words = [w for w in re.split(r"\s+", (kw or "").strip()) if w][:8]
    if not words:
        return []
    score_sql = " + ".join(
        f"(CASE WHEN title LIKE ? OR content LIKE ? OR project LIKE ? THEN 1 ELSE 0 END)"
        for _ in words)
    args: list = []
    for w in words:
        like = f"%{w}%"
        args += [like, like, like]
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT *, ({score_sql}) AS score FROM records WHERE status='active' AND "
            f"({' OR '.join(['(title LIKE ? OR content LIKE ? OR project LIKE ?)'] * len(words))}) "
            "ORDER BY score DESC, date DESC, id DESC LIMIT ?",
            args + args + [max(1, min(limit, 100))])]


# ---------------------------------------------------------------- 大脑记忆（核心）

def add_memory(root: str, content: str, kind: str = "note", tags: str = "",
               project: str = "", agent: str = "", pinned: bool = False) -> int:
    if kind not in KINDS:
        kind = "note"
    with db_conn(root) as conn:
        agent = _norm_agent(agent)
        _upsert_agent(conn, agent, project)
        cur = conn.execute(
            "INSERT INTO memories(kind,content,tags,project,agent,pinned,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (kind, content, tags.strip(), project, agent, 1 if pinned else 0, _now(), _now()))
        return cur.lastrowid


PUSH_LIMIT_PROJECT = 3   # 记忆推送：项目相关最多条数（项目教训优先于全局置顶）
PUSH_LIMIT_PINNED = 3    # 记忆推送：全局置顶最多条数（防置顶膨胀推高每次心跳成本）
PUSH_LIMIT_RELATED = 2   # 记忆推送：跨项目语义联想最多条数（联想阈值 0.12）
PUSH_CHAR_CAP = 600      # 记忆推送：输出总字数封顶
PUSH_THROTTLE_MIN = 10   # 同 agent+project 重复心跳的免推窗口（分钟）——推送内容还在会话上下文里


def recall_for(root: str, project: str) -> list:
    """开工记忆推送（反射弧）三层：本项目相关 → 全局置顶 → 跨项目语义联想
    （该项目最近记录的 bigram 词集与其他项目记忆算 Jaccard，撞到别处踩过的坑自动想起）。
    命中即记一次唤起（use_count+1）——只对实际推送出去的条目计数（用进废退）。"""
    with db_conn(root) as conn:
        proj = [dict(r) for r in conn.execute(
            "SELECT * FROM memories WHERE status='active' AND project=? "
            "ORDER BY use_count DESC, id DESC LIMIT ?",
            (project or "", PUSH_LIMIT_PROJECT))]
        pins = [dict(r) for r in conn.execute(
            "SELECT * FROM memories WHERE status='active' AND pinned=1 "
            "AND (project='' OR project IS NULL OR project!=?) "
            "ORDER BY use_count DESC, id DESC LIMIT ?",
            (project or "", PUSH_LIMIT_PINNED))]
        # 跨项目语义联想：本项目最近 3 条记录的词集 vs 其他项目记忆
        related: list = []
        cur_txt = " ".join(r["content"] for r in conn.execute(
            "SELECT content FROM (SELECT content FROM records WHERE status='active' AND project=? "
            "ORDER BY id DESC LIMIT 3)", (project or "",)))
        cur_toks = _tokens(cur_txt)
        if len(cur_toks) >= 4:
            others = [dict(r) for r in conn.execute(
                "SELECT * FROM memories WHERE status='active' AND project IS NOT NULL "
                "AND project!='' AND project!=? LIMIT 300", (project or "",))]
            scored = []
            for om in others:
                ot = _tokens(om["content"])
                if not ot:
                    continue
                sim = len(cur_toks & ot) / len(cur_toks | ot)
                if sim >= 0.12:
                    scored.append((sim, om))
            scored.sort(key=lambda x: -x[0])
            for sim, om in scored[:PUSH_LIMIT_RELATED]:
                om["related_project"] = om["project"]
                related.append(om)
        rows, seen, total = [], set(), 0
        for r in proj + pins + related:  # 项目相关 → 置顶 → 跨项目联想
            if r["id"] in seen:
                continue
            if total >= PUSH_CHAR_CAP:
                break
            seen.add(r["id"])
            total += len(r["content"])
            rows.append(r)
        now = _now()
        for r in rows:
            conn.execute("UPDATE memories SET use_count=use_count+1, last_hit=? WHERE id=?",
                         (now, r["id"]))
            r["use_count"] = (r["use_count"] or 0) + 1  # 返回值反映本次唤起后的计数
            r["last_hit"] = now
        return rows


def should_push(root: str, agent: str, project: str) -> bool:
    """同一 agent+project 在免推窗口内的重复心跳不再推送（上下文里已有，重推纯冗余）。"""
    agent = _norm_agent(agent)
    with db_conn(root) as conn:
        row = conn.execute("SELECT ts, project FROM sessions WHERE agent=?", (agent,)).fetchone()
    if not row or row["project"] != (project or ""):
        return True
    try:
        age = (datetime.datetime.now() - datetime.datetime.fromisoformat(row["ts"])).total_seconds()
    except (ValueError, TypeError):
        return True
    return age >= PUSH_THROTTLE_MIN * 60


def log_search(root: str, tool: str, query: str, hits: int, agent: str = "") -> None:
    """检索日志（反馈回路）：谁/何时/用什么工具/查什么/命中几条。失败不影响检索本身。"""
    try:
        with db_conn(root) as conn:
            conn.execute("INSERT INTO searches(ts,agent,tool,query,hits) VALUES(?,?,?,?,?)",
                         (_now(), agent or "", tool, (query or "")[:200], max(0, int(hits or 0))))
    except sqlite3.Error:
        pass


def search_memories(root: str, query: str = "", kind: str = "", limit: int = 20) -> list:
    """检索记忆：多词评分（命中词数，与 records 检索一致）；无 query 时置顶优先按 id 倒序。"""
    words = [w for w in re.split(r"\s+", (query or "").strip()) if w][:8]
    q = "SELECT * FROM memories WHERE status='active'"
    args: list = []
    if words:
        conds, score_sql = [], []
        for w in words:
            like = f"%{w}%"
            conds.append("(content LIKE ? OR tags LIKE ? OR project LIKE ?)")
            args += [like, like, like]
            score_sql.append("(CASE WHEN content LIKE ? THEN 2 ELSE 0 END + CASE WHEN tags LIKE ? THEN 1 ELSE 0 END)")
            args += [like, like]
        q += " AND (" + " OR ".join(conds) + ")"
        if kind in KINDS:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY pinned DESC, (" + " + ".join(score_sql) + ") DESC, id DESC LIMIT ?"
        args.append(max(0, min(limit, 100)))
    else:
        if kind in KINDS:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY pinned DESC, id DESC LIMIT ?"
        args.append(max(0, min(limit, 100)))
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(q, args)]


def edit_memory(root: str, mid: int, content: str = "", kind: str = "", tags: str = "",
                pinned: bool | None = None) -> str:
    """按 id 更新记忆字段（只改传入的）。返回错误或 ""。"""
    sets, args = [], []
    if content:
        sets.append("content=?")
        args.append(content)
    if kind in KINDS:
        sets.append("kind=?")
        args.append(kind)
    if tags is not None and tags != "":
        sets.append("tags=?")
        args.append(tags.strip())
    if pinned is not None:
        sets.append("pinned=?")
        args.append(1 if pinned else 0)
    if not sets:
        return "没有要更新的字段"
    with db_conn(root) as conn:
        cur = conn.execute(f"UPDATE memories SET {', '.join(sets)}, updated=? WHERE id=?",
                           (*args, _now(), mid))
        if cur.rowcount == 0:
            return f"记忆 #{mid} 不存在"
    return ""


def delete_memory(root: str, mid: int) -> str:
    with db_conn(root) as conn:
        cur = conn.execute("UPDATE memories SET status='deleted' WHERE id=?", (mid,))
        if cur.rowcount == 0:
            return f"记忆 #{mid} 不存在"
    return ""


# ---------------------------------------------------------------- 项目

def list_projects(root: str, limit: int = 50) -> list:
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT p.name, p.agent, p.created, p.status,"
            " (SELECT MAX(date) FROM records r WHERE r.project=p.name AND r.status='active') AS last_active,"
            " (SELECT COUNT(*) FROM records r WHERE r.project=p.name AND r.status='active') AS n_records"
            " FROM projects p ORDER BY last_active DESC, p.name LIMIT ?",
            (max(1, min(limit, 200)),))]


def get_project(root: str, name: str) -> dict | None:
    with db_conn(root) as conn:
        proj = conn.execute("SELECT name, agent, created FROM projects WHERE name=?", (name,)).fetchone()
        if not proj:
            return None
        recs = [dict(r) for r in conn.execute(
            "SELECT date,agent,title,content FROM records WHERE project=? AND status='active' "
            "ORDER BY date DESC, id DESC LIMIT 5", (name,))]
        return {"name": proj["name"], "agent": proj["agent"], "created": proj["created"],
                "last_active": conn.execute(
                    "SELECT MAX(date) AS d FROM records WHERE project=? AND status='active'",
                    (name,)).fetchone()["d"] or "", "records": recs}


def project_exists(root: str, name: str) -> bool:
    with db_conn(root) as conn:
        return conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone() is not None


# ---------------------------------------------------------------- 检索与进度

def get_progress(root: str, limit: int = 30) -> list:
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT date,project,agent,title FROM records WHERE status='active' "
            "ORDER BY date DESC, id DESC LIMIT ?", (max(1, min(limit, 200)),))]


def search_all(root: str, kw: str, limit: int = 30) -> dict:
    """全脑检索：工作记录 + 记忆 + 文件名。返回 {"records": [...], "memories": [...], "files": [...]}"""
    if not kw.strip():
        return {"records": [], "memories": [], "files": []}
    return {"records": search_records(root, kw, limit),
            "memories": search_memories(root, kw, limit),
            "files": search_files(root, kw, limit)}


def search_files(root: str, kw: str, limit: int = 20) -> list:
    words = [w for w in re.split(r"\s+", (kw or "").strip()) if w][:8]
    if not words:
        return []
    conds = " OR ".join(["name LIKE ?"] * len(words))
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT project,name,path FROM files WHERE ({conds}) LIMIT ?",
            [f"%{w}%" for w in words] + [max(1, min(limit, 100))])]


def update_files_index(root: str, rows: list) -> None:
    """GUI 扫描后刷新文件索引。rows: [(project, path, name, grp, size, mtime)]"""
    if not rows:
        return
    with db_conn(root) as conn:
        conn.execute("DELETE FROM files")
        conn.executemany("INSERT OR REPLACE INTO files(project,path,name,grp,size,mtime) VALUES(?,?,?,?,?,?)",
                         rows[:2000])


def stats(root: str) -> dict:
    with db_conn(root) as conn:
        def one(sql, args=()):
            return conn.execute(sql, args).fetchone()[0]
        today = datetime.date.today().isoformat()
        agent_counts = {r["agent"] or "其他": r["n"] for r in conn.execute(
            "SELECT agent, COUNT(*) AS n FROM records WHERE status='active' GROUP BY agent ORDER BY n DESC")}
        monthly = {r["m"]: r["n"] for r in conn.execute(
            "SELECT substr(date,1,7) AS m, COUNT(*) AS n FROM records "
            "WHERE status='active' AND date!='' GROUP BY m ORDER BY m DESC LIMIT 6")}
        return {"projects": one("SELECT COUNT(*) FROM projects"),
                "records": one("SELECT COUNT(*) FROM records WHERE status='active'"),
                "memories": one("SELECT COUNT(*) FROM memories WHERE status='active'"),
                "today": one("SELECT COUNT(*) FROM records WHERE status='active' AND date=?", (today,)),
                "errors_open": one("SELECT COUNT(*) FROM errors WHERE status='open'"),
                "searches_today": one("SELECT COUNT(*) FROM searches WHERE substr(ts,1,10)=?", (today,)),
                "searches_total": one("SELECT COUNT(*) FROM searches"),
                "projects_stalled": one("SELECT COUNT(*) FROM projects WHERE status='stalled'"),
                "agent_counts": agent_counts, "monthly_counts": monthly}


# ---------------------------------------------------------------- 流水 / 错误 / 会话

def journal_add(root: str, agent: str, action: str, target: str = "",
                backup: str = "", note: str = "") -> None:
    try:
        with db_conn(root) as conn:
            conn.execute("INSERT INTO journal(ts,agent,action,target,backup,note) VALUES(?,?,?,?,?,?)",
                         (_now(), agent, action, target, backup, note))
    except Exception:  # noqa: BLE001 流水是旁路
        pass


def journal_list(root: str, limit: int = 200) -> list:
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM journal ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))]


def error_add(root: str, agent: str, title: str, detail: str = "",
              project: str = "", undo: str = "") -> str:
    if not title or not title.strip():
        return "title 必填"
    with db_conn(root) as conn:
        agent = _norm_agent(agent)
        _upsert_agent(conn, agent, project)
        conn.execute("INSERT INTO errors(ts,agent,project,title,detail,undo,status) VALUES(?,?,?,?,?,?,'open')",
                     (_now(), agent or "unknown", project, title.strip()[:200], (detail or "")[:4000], undo))
    return ""


def error_list(root: str, status: str = "", limit: int = 100) -> list:
    q = "SELECT * FROM errors"
    args: list = []
    if status in ("open", "fixed"):
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(limit, 300)))
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(q, args)]


def error_set_status(root: str, error_id: int, status: str) -> str:
    if status not in ("open", "fixed"):
        return "status 只能是 open/fixed"
    with db_conn(root) as conn:
        cur = conn.execute("UPDATE errors SET status=? WHERE id=?", (status, error_id))
        if cur.rowcount == 0:
            return f"未找到 id={error_id} 的错误"
    return ""


def heartbeat_touch(root: str, agent: str, project: str = "", note: str = "") -> tuple:
    """更新心跳并重建会话表（清掉陈旧条目），返回 (错误或"", 同项目其他活跃会话)。"""
    agent = _norm_agent(agent)
    if not agent:
        return "agent 必填", []
    now = datetime.datetime.now()
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM sessions")]
        fresh = []
        for s in rows:
            if s["agent"] == agent:
                continue
            try:
                ts = datetime.datetime.fromisoformat(s["ts"])
            except (ValueError, TypeError):
                continue
            if (now - ts).total_seconds() < SESSION_TTL:
                fresh.append(s)
        conn.execute("DELETE FROM sessions")  # 重建语义：陈旧会话一并清除
        for s in fresh:
            conn.execute("INSERT INTO sessions(agent,project,note,ts) VALUES(?,?,?,?)",
                         (s["agent"], s["project"], s["note"], s["ts"]))
        conn.execute("INSERT INTO sessions(agent,project,note,ts) VALUES(?,?,?,?)",
                     (agent.strip(), project or "", note or "", now.isoformat(timespec="seconds")))
        _upsert_agent(conn, agent, project, beat=True)
    active = [s for s in fresh
              if project and s.get("project") == project
              and _ts_age(s.get("ts", ""), now) < SESSION_ACTIVE]
    return "", active


def _ts_age(ts: str, now: datetime.datetime) -> float:
    try:
        return (now - datetime.datetime.fromisoformat(ts)).total_seconds()
    except (ValueError, TypeError):
        return 1e9


def active_sessions(root: str) -> list:
    now = datetime.datetime.now()
    with db_conn(root) as conn:
        out = []
        for s in conn.execute("SELECT * FROM sessions"):
            d = dict(s)
            if _ts_age(d.get("ts", ""), now) < SESSION_TTL:
                out.append(d)
        return sorted(out, key=lambda s: s.get("ts", ""), reverse=True)


def list_agents(root: str) -> list:
    """注册 agent 全表 + 在线状态（sessions 存活者打 live 标），按最近活跃排序。"""
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY last_seen DESC")]
    online = {s["agent"] for s in active_sessions(root)}
    for r in rows:
        r["online"] = r["name"] in online
    return rows


# ---------------------------------------------------------------- 备份

def backup_brain(root: str, keep: int = BACKUP_KEEP) -> str:
    """在线备份 brain.db 到 _hub/brain-backups/（保留 keep 份轮转）。返回错误或备份路径。"""
    src = db_path(root)
    if not src.is_file():
        return "大脑数据库不存在"
    bdir = src.parent / BACKUP_DIR
    try:
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = bdir / f"brain-{stamp}.db"
        n = 1
        while dst.exists():  # 同秒多次备份防同名覆盖
            dst = bdir / f"brain-{stamp}({n}).db"
            n += 1
        sconn = sqlite3.connect(str(src))
        dconn = sqlite3.connect(str(dst))
        with dconn:
            sconn.backup(dconn)
        dconn.close()
        sconn.close()
        baks = sorted(bdir.glob("brain-*.db"))
        for old in baks[:-keep] if len(baks) > keep else []:
            old.unlink()
        return str(dst)
    except (sqlite3.Error, OSError) as e:
        return f"备份失败：{e}"


# ---------------------------------------------------------------- 智能层（零依赖本地算法：待办提取 / 相似检测 / 体检报告）

# 引导词必须紧贴冒号才算待办句式：否则「待办勾销闭环：」「两条'待办'系误抓」
# 这类复合词/引用全部误抓（2026-10-02 实锤），宁可漏自然语句不可滥报
_TODO_RE = re.compile(
    r"(待办事项|待办|后续|下次|下一步|TODO|待确认|待验证|待实测|需要再|记得|提醒用户|尚未完成|遗留问题|待人工|待续)"
    r"[：:]([^\n]{4,120})")
_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set:
    """中文 bigram + 拉丁词的轻量分词（零依赖），供相似度计算。"""
    text = re.sub(r"[^\w一-鿿]+", " ", (text or "").lower())
    toks: set = set()
    for seg in text.split():
        if _WORD_RE.fullmatch(seg):
            toks.add(seg)
        else:
            toks.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return toks


def extract_todos(root: str, limit: int = 20) -> list:
    """元认知：扫描工作记录里的待办/承诺线索（后续/待验证/下一步…句式），
    返回 [{id, project, date, agent, todo}]——大脑不只记过去，还管欠账。"""
    out = []
    with db_conn(root) as conn:
        rows = conn.execute(
            "SELECT id, project, date, agent, content FROM records "
            "WHERE status='active' ORDER BY id DESC LIMIT 400").fetchall()
        done = {(d[0], d[1]) for d in conn.execute("SELECT record_id, todo FROM todos_done")}
    for r in rows:
        for m in _TODO_RE.finditer(r["content"] or ""):
            text = m.group(2).strip()
            while text and text[0] in "】』」)]：:，。；、*#>\"'“”‘’":
                text = text[1:].strip()
            text = text.strip("，。；、")
            if len(text) < 4:
                continue
            todo_str = f"{m.group(1)}：{text}"
            # 勾销匹配用前缀包含：调用方可能传提取串原样，也可能从 hub_list_todos 行里截片段
            if any(rid == r["id"] and (kt[:60] in todo_str or todo_str[:60] in kt)
                   for rid, kt in done):
                continue  # 已勾销的待办不再出现（闭环）
            out.append({"id": r["id"], "project": r["project"], "date": r["date"],
                        "agent": r["agent"], "todo": todo_str})
            if len(out) >= max(1, min(limit, 100)):
                return out
    return out


def get_record(root: str, record_id: int) -> dict:
    """单条记录全文读取：hub_search/distill 只给 gist，蒸馏与复盘精读需要全文。"""
    try:
        rid = int(record_id)
    except (TypeError, ValueError):
        return {}
    if rid <= 0:
        return {}
    with db_conn(root) as conn:
        row = conn.execute(
            "SELECT id, project, agent, date, title, content, status FROM records WHERE id=?",
            (rid,)).fetchone()
    return dict(row) if row else {}


def mark_todo_done(root: str, record_id: int, todo: str, agent: str = "") -> str:
    """勾销待办线索（闭环）：勾销后 extract_todos 不再返回该条。todo 传
    extract_todos 返回的原文（或 hub_list_todos 行内的待办串）。"""
    if not todo or not todo.strip():
        return "todo 必填"
    with db_conn(root) as conn:
        conn.execute("INSERT INTO todos_done(record_id,todo,agent,ts) VALUES(?,?,?,?)",
                     (record_id, todo.strip()[:200], _norm_agent(agent) or "", _now()))
    return ""


_CONFLICT_WORDS = ("取代", "纠正", "错误", "不对", "实际是", "应为", "真相", "误判", "作废")


def _verdict(ca: str, cb: str, kind_same: bool) -> str:
    """裁决辅助：恰一条含修正词 → 疑似矛盾（一条修正另一条）；同 kind → 疑似重复。"""
    ca, cb = ca or "", cb or ""
    wa, wb = any(w in ca for w in _CONFLICT_WORDS), any(w in cb for w in _CONFLICT_WORDS)
    if wa != wb:
        return "疑似矛盾（一条修正另一条）"
    return "疑似重复" if kind_same else "相关"


def similar_memories(root: str, threshold: float = 0.55, limit: int = 10) -> list:
    """巩固：两两 Jaccard 相似度检测疑似重复/矛盾记忆（bigram 分词，<=500 条时全算）。
    返回 [{a, b, sim, verdict, content_a, content_b}]——verdict 为裁决辅助建议，最终由 agent/用户裁决。"""
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, kind, content FROM memories WHERE status='active' ORDER BY id")]
    toks = [(r["id"], r["kind"], r["content"], _tokens(r["content"])) for r in rows]
    out = []
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            ta, tb = toks[i][3], toks[j][3]
            if not ta or not tb:
                continue
            inter = len(ta & tb)
            sim = inter / len(ta | tb) if ta | tb else 0.0
            if sim >= threshold:
                out.append({"a": toks[i][0], "b": toks[j][0], "sim": round(sim, 2),
                            "verdict": _verdict(toks[i][2], toks[j][2], toks[i][1] == toks[j][1]),
                            "content_a": toks[i][2][:80], "content_b": toks[j][2][:80]})
            if len(out) >= max(1, min(limit, 50)):
                return out
    out.sort(key=lambda x: -x["sim"])
    return out


def health_report(root: str) -> dict:
    """大脑体检：汇总各智能维度 + 数据规模，供 hub_health 工具与 GUI 体检摘要。"""
    s = stats(root)
    todos = extract_todos(root, limit=50)
    sims = similar_memories(root, limit=20)
    with db_conn(root) as conn:
        bare_titles = conn.execute(
            "SELECT COUNT(*) FROM records WHERE status='active' "
            "AND title GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]（*）'").fetchone()[0]
    return {"records": s["records"], "memories": s["memories"], "projects": s["projects"],
            "projects_stalled": s.get("projects_stalled", 0),
            "errors_open": s.get("errors_open", 0),
            "searches_today": s.get("searches_today", 0), "searches_total": s.get("searches_total", 0),
            "todos": todos, "todo_count": len(todos),
            "dup_memories": sims, "dup_memory_count": len(sims),
            "bare_titles": bare_titles,
            "crystallization": round(s["memories"] * 100 / max(1, s["records"]), 1)}


# ---------------------------------------------------------------- 环境档案（本机设置的结构化登记）

def env_set(root: str, category: str, key: str, value: str, agent: str = "") -> str:
    """登记/更新一条本机环境配置（UPSERT）。category：系统/网络/工具/路径/配置…"""
    if not category.strip() or not key.strip():
        return "category 与 key 必填"
    with db_conn(root) as conn:
        conn.execute(
            "INSERT INTO env_items(category,key,value,agent,updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value, "
            "agent=excluded.agent, updated=excluded.updated",
            (category.strip()[:40], key.strip()[:120], (value or "")[:2000],
             _norm_agent(agent), _now()))
    return ""


def env_list(root: str, category: str = "", kw: str = "", limit: int = 200) -> list:
    q = "SELECT * FROM env_items WHERE 1=1"
    args: list = []
    if category.strip():
        q += " AND category=?"
        args.append(category.strip())
    if kw.strip():
        q += " AND (key LIKE ? OR value LIKE ? OR category LIKE ?)"
        args += [f"%{kw.strip()}%"] * 3
    q += " ORDER BY category, key LIMIT ?"
    args.append(max(1, min(limit, 500)))
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(q, args)]


def env_scan(root: str, agent: str = "") -> int:
    """自动采集本机基础配置快照写入环境档案（幂等 UPSERT，仅标准库）。返回写入条数。"""
    import platform
    import shutil
    import sys
    import winreg
    items: list = []
    items.append(("系统", "主机名", platform.node()))
    items.append(("系统", "操作系统", f"{platform.system()} {platform.release()} ({platform.version()})"))
    items.append(("工具", "Python", f"{sys.executable} ({sys.version.split()[0]})"))
    git = shutil.which("git")
    if git:
        try:
            import subprocess
            gv = subprocess.run([git, "--version"], capture_output=True, timeout=5)
            items.append(("工具", "Git", f"{git} ({gv.stdout.decode('utf-8', 'replace').strip().split()[-1]})"))
        except Exception:  # noqa: BLE001
            items.append(("工具", "Git", git))
    ff = shutil.which("ffmpeg")
    if ff:
        items.append(("工具", "FFmpeg", ff))
    if (Path.home() / ".agenthub" / "bin" / "es.exe").is_file():
        items.append(("工具", "Everything(ES)", str(Path.home() / ".agenthub" / "bin" / "es.exe")))
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            proxy = winreg.QueryValueEx(k, "ProxyServer")[0]
            enable = winreg.QueryValueEx(k, "ProxyEnable")[0]
        except OSError:
            proxy, enable = "", 0
        items.append(("网络", "系统代理", proxy if enable else "未启用"))
    except OSError:
        items.append(("网络", "系统代理", "读取失败"))
    envp = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    items.append(("网络", "环境变量代理", envp or "未设置"))
    for drv in ("C:", "D:", "E:", "F:"):
        try:
            import shutil as _sh
            total, _u, free = _sh.disk_usage(drv + chr(92))
            items.append(("磁盘", f"{drv} 剩余", f"{free // 2**30}G / 总 {total // 2**30}G"))
        except OSError:
            pass
    n = 0
    for cat, k2, v in items:
        if not env_set(root, cat, k2, v, agent):
            n += 1
    return n


def distill_candidates(root: str, limit: int = 20) -> list:
    """记忆蒸馏候选（结晶流水线）：content 含「目的」结论但同项目无相似记忆覆盖的记录。
    行业头部（mem0/腾讯AgentMemory）用 LLM 蒸馏；本方案零成本规则版——目的行提取 +
    bigram 相似度查重，候选经 agent/用户确认后 hub_memory_write 沉淀为记忆。"""
    with db_conn(root) as conn:
        recs = [dict(r) for r in conn.execute(
            "SELECT id, project, date, agent, content FROM records "
            "WHERE status='active' ORDER BY id DESC LIMIT 300")]
        mems = [dict(r) for r in conn.execute(
            "SELECT project, content FROM memories WHERE status='active'")]
    mem_toks: dict = {}
    for m in mems:
        mem_toks.setdefault(m["project"], []).append(_tokens(m["content"]))
    out = []
    for r in recs:
        m = re.search(r"目的[】\]:：]\s*(.+)", r["content"] or "")
        if not m:
            continue
        gist = m.group(1).strip().strip("【】")
        if len(gist) < 6:
            continue
        # 复合任务（①②③…/分号）按片段级查重：一条记录可能只蒸馏了一部分，
        # 候选只展示「尚未被记忆覆盖」的片段（2026-10-02 蒸馏演示实测发现）
        segs = [s.strip() for s in re.split(r"[①②③④⑤⑥⑦⑧⑨⑩]|；", gist)
                if len(s.strip()) >= 6] or [gist]
        project_mems = mem_toks.get(r["project"], [])
        uncovered = []
        for seg in segs:
            st = _tokens(seg)
            if not st:
                continue
            # containment（片段被记忆包含的比例）而非 Jaccard——短片段 vs 长记忆时
            # Jaccard 被记忆大词集稀释，会误判未覆盖（2026-10-02 蒸馏演示实测）
            covered = any(
                (len(st & mt) / len(st) if st else 0) >= 0.3
                for mt in project_mems)
            if not covered:
                uncovered.append(seg.strip())
        if uncovered:
            out.append({"id": r["id"], "project": r["project"], "date": r["date"],
                        "agent": r["agent"], "gist": "；".join(uncovered)[:80]})
        if len(out) >= max(1, min(limit, 50)):
            return out
    return out