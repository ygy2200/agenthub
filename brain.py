# -*- coding: utf-8 -*-
"""AgentHub 大脑：SQLite 结构化存储层（记忆/工作记录/流水/错误/会话/文件索引）。

设计：
- brain.db 位于 <root>/_hub/brain.db，WAL 模式，GUI 与各 agent 的 MCP server 多进程并发安全
- 历史 md/jsonl/json 增量迁移入库（幂等可重复跑），原文件保留为只读存档；此后 DB 是唯一真理
- 记忆五类 kind：fact 环境事实 / preference 用户偏好 / lesson 踩坑经验 / project 项目进展 / note 随手记
- 本模块 import core（复用记录解析），core 不反向 import 本模块（core 的流水埋点走 JOURNAL_SINK 注入）
"""
from __future__ import annotations

import atexit
import datetime
import json
import math
import os
import re
import sqlite3
import sys
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import core

DB_NAME = "brain.db"
BACKUP_DIR = "brain-backups"
BACKUP_KEEP = 30
KINDS = ("fact", "preference", "lesson", "project", "note")
# M6 记忆成熟度（2026-10-12，ByteRover 模式）：记忆不是生来平等——新写入是草稿，
# 被反复用/人工确认过的才敢依赖。与 salience（显著度权重）互补：那是「多响」，这是「多信」。
# 晋升只上不下（core 不降级——人工 pinned 的信任不该被低频推翻）。
MATURITY_LEVELS = ("draft", "validated", "core")
VALIDATE_AT = 3    # 被唤起/检索命中够这么多次 → validated
CORE_AT = 10       # pinned 且够这么多次 → core
KIND_CN = {"fact": "环境事实", "preference": "用户偏好", "lesson": "踩坑经验",
           "project": "项目进展", "note": "随手记", "error": "踩坑登记"}
# P1-2 记录类型枚举（schema 收口）：写入即校验；含「## 决策记录」段自动推断 decision
RECORD_TYPES = ("work_log", "decision", "todo", "note", "crystal")
RECORD_TYPE_CN = {"work_log": "工作记录", "decision": "决策", "todo": "待办", "note": "随记"}
SESSION_TTL = 1800     # 「在线」判定窗口（秒）：active_sessions/hub_status/list_agents 打 live 标用
SESSION_ACTIVE = 300   # 同项目"正在工作"判定窗口（秒）
SESSION_RETAIN = 24 * 3600  # sessions 表保留窗口（秒）：**只用于心跳时的陈旧剪枝**，与「在线」解耦
# 两窗口为何分开（2026-10-11）：剪枝原先用 SESSION_TTL（30 分钟），导致会话记录最多活 30 分钟——
# GUI 总览「最近会话」即使把展示窗口放宽到 24h 也拿不到更早的数据（每次心跳都把旧行删了）。
# 在线判定（30 分钟）回答的是「这 agent 还活着吗」；保留窗口回答的是「最近谁在哪干过」——
# 是展示层的事，不该由在线语义决定。故保留放宽到 24h，在线判定与推送节流均不受影响。
STALL_DAYS = 90        # 项目停滞判定：超过该天数无记录即 stalled
MAX_CONTENT = 128 * 1024

# 记忆衰减（2026-10-04 引入，参数取自 ai-memory decay.rs；改动须过 adv_brain 回归）
DECAY_PARAMS = {
    "lam": 0.02,            # 年龄衰减系数 ≈35 天半衰期
    "sigma": 0.6,           # 访问强化权重
    "mu": 0.04,             # 距上次访问的衰减系数
    "salience_default": 1.0,
    "salience_min": 0.25,
    "salience_max": 2.0,
    "cold_threshold": 0.20,
    "breadth_weight": 0.0,  # 0.0 = 恒等，不改变任何现有排序
}


def db_path(root: str) -> Path:
    return Path(root) / core.DIR_META / DB_NAME


# ── WAL 常驻哨兵（2026-10-09 实测修复）──────────────────────────────────────
# 现象：Windows 上短连接频繁开关时，最后一个连接关闭会删除 brain.db-wal/-shm，
# 并发线程（8 线程）恰在此时打开库 → 拿到只读句柄 → 写语句抛
# "attempt to write a readonly database"，心跳/记录静默丢失（实测 6~8/8 才成功）。
# 修法：进程内为已存在的库常驻一条连接，让 -wal 不被反复删除（实测 8 轮 ×8 线程全 8/8）。
_SENTINELS: dict[str, sqlite3.Connection] = {}
_SENTINEL_LOCK = threading.Lock()
_SENTINEL_MAX = 8  # 句柄上限：临时库（测试）增多时淘汰最旧的，避免句柄堆积


def _keep_wal(root: str) -> None:
    """为已存在的 brain.db 维持一条常驻连接（幂等、失败静默、永不建库）。"""
    p = str(db_path(root))
    if not os.path.exists(p):
        return
    with _SENTINEL_LOCK:
        c = _SENTINELS.get(p)
        if c is not None:
            try:
                c.execute("SELECT 1").fetchone()
                return
            except sqlite3.Error:
                _SENTINELS.pop(p, None)
        if len(_SENTINELS) >= _SENTINEL_MAX:
            for k in list(_SENTINELS)[:max(1, _SENTINEL_MAX // 2)]:
                try:
                    _SENTINELS.pop(k).close()
                except Exception:
                    pass
        try:
            c = sqlite3.connect(p, timeout=15, check_same_thread=False)
            _apply_pragmas(c)
            _SENTINELS[p] = c
        except sqlite3.Error:
            pass


def _apply_pragmas(conn) -> None:
    """连接级 PRAGMA（单一来源：常驻哨兵与短连接都走这里，防两边配置漂移）。

    busy_timeout 由 5000 → 15000：业界明确点名默认 5s 在「一个进程写 + 另一个进程读/
    HTTP 服务并发访问」下不够（mcp-memory-service 多客户端文档把 5s 列为根因，推荐 15s）。
    synchronous=NORMAL：WAL 下的推荐档（完全持久化语义保留，只把 fsync 交给 WAL 检查点），
    比 FULL 快且不增加损坏风险——断电只可能丢最后一个检查点之后的事务，不丢库。
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error as e:
        # 留痕（2026-10-13 吞异常审计补）：PRAGMA 静默失败 = 并发配置没生效，
        # 症状是"偶发 database is locked"却查不到原因——必须有人能看见它失败过。
        try:
            print(f"[agenthub] PRAGMA 应用失败（{e}），该连接可能未启用 WAL/15s 超时",
                  file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001
            pass


def _close_sentinels() -> None:
    """进程退出前收尾：checkpoint + 关闭常驻连接，不留 -wal/-shm 残留。

    对应业界已知事故（ruvnet/ruflo#3397，2026-09）：常驻 WAL 句柄若从不释放，
    残留的 -wal/-shm 会让**其他进程**的首次写入被拒（对方无法区分"正写着"和"握着不用"），
    只能重启 + 手工 checkpoint 才恢复。我们的常驻哨兵是刻意为之（防 Windows 删 wal），
    但**必须有退出钩子**——否则就变成那起事故的形状。
    """
    with _SENTINEL_LOCK:
        for _p, c in list(_SENTINELS.items()):
            try:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:  # noqa: BLE001  退出路径不抛
                pass
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        _SENTINELS.clear()


atexit.register(_close_sentinels)


def release_root(root: str) -> None:
    """关闭该库在本进程的常驻连接（测试清理临时目录前调用，避免 Windows 文件占用）。"""
    with _SENTINEL_LOCK:
        c = _SENTINELS.pop(str(db_path(root)), None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


# B12：此处**曾是双重注册**——下面还有个 _close_all_sentinels 也注册了 atexit。
# atexit 是 LIFO（后注册先执行），于是「只 close 不做 checkpoint」的那个先跑并清空
# _SENTINELS 字典，等轮到本函数时字典已空 → **checkpoint 在真实退出时从未执行过**，
# 只留下 close。这正好是上面注释里警告的 ruvnet/ruflo#3397 事故形状（-wal/-shm 残留
# 会拒掉其他进程的首次写入）。之所以一直没暴露：回归里是**直接调用** _close_sentinels()，
# 绕过了 atexit 顺序，所以测试绿而线上漏。现已删除第二个钩子，保证退出走带 checkpoint 的这条路。


@contextmanager
def db_conn(root: str):
    """打开一个短连接（WAL + busy_timeout），用完即关。多进程并发安全。
    不主动创建目录/文件——建库责任在 init_db，无效 root 直接抛错。
    打开前先确保本进程有常驻连接（防 Windows 上 -wal 反复删建导致的只读句柄）。"""
    p = db_path(root)
    _keep_wal(root)
    conn = sqlite3.connect(str(p), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        _apply_pragmas(conn)
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
    created TEXT DEFAULT "",
    record_type TEXT DEFAULT "",
    valid_from TEXT DEFAULT "",
    valid_until TEXT DEFAULT "",
    superseded_by INTEGER DEFAULT 0
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
    last_hit TEXT DEFAULT "",
    salience REAL DEFAULT 1.0,
    superseded_by INTEGER DEFAULT 0,
    valid_until TEXT DEFAULT "",
    confidence REAL DEFAULT 1.0,
    evidence TEXT DEFAULT "",
    maturity TEXT DEFAULT "draft"
);
CREATE TABLE IF NOT EXISTS memory_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mid INTEGER NOT NULL,
    action TEXT DEFAULT "",
    before TEXT DEFAULT "",
    after TEXT DEFAULT "",
    agent TEXT DEFAULT "",
    created TEXT DEFAULT ""
);
CREATE INDEX IF NOT EXISTS idx_memrev_mid ON memory_revisions(mid);
CREATE TABLE IF NOT EXISTS tag_schema (
    tag TEXT PRIMARY KEY,
    use_count INTEGER DEFAULT 0,
    first_seen TEXT DEFAULT "",
    last_seen TEXT DEFAULT ""
);
-- P1-3 关系 + 属性（双链/引用图底座）：relations=有向边（正文 [[X]] 自动抽 mention 边）；
-- attrs=节点属性（node_kind∈project/record/memory/note；project 上 is_inheritable=1 的属性被子节点继承）
CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_kind TEXT DEFAULT "",
    from_id INTEGER DEFAULT 0,
    to_kind TEXT DEFAULT "",
    to_ref TEXT DEFAULT "",
    kind TEXT DEFAULT "mention",
    created TEXT DEFAULT "",
    UNIQUE(from_kind, from_id, to_kind, to_ref, kind)
);
CREATE INDEX IF NOT EXISTS idx_rel_from ON relations(from_kind, from_id);
CREATE INDEX IF NOT EXISTS idx_rel_to ON relations(to_kind, to_ref);
CREATE TABLE IF NOT EXISTS attrs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_kind TEXT DEFAULT "",
    node_ref TEXT DEFAULT "",
    key TEXT DEFAULT "",
    value TEXT DEFAULT "",
    is_inheritable INTEGER DEFAULT 0,
    created TEXT DEFAULT "",
    UNIQUE(node_kind, node_ref, key)
);
CREATE INDEX IF NOT EXISTS idx_attrs_node ON attrs(node_kind, node_ref);
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
CREATE TABLE IF NOT EXISTS op_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT "",
    agent TEXT DEFAULT "",
    tool TEXT DEFAULT "",
    project TEXT DEFAULT "",
    summary TEXT DEFAULT "",
    undo_kind TEXT DEFAULT "",
    undo_ref TEXT DEFAULT ""
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT DEFAULT "",
    title TEXT NOT NULL,
    detail TEXT DEFAULT "",
    status TEXT DEFAULT "pending",
    owner TEXT DEFAULT "",
    claimed_at TEXT DEFAULT "",
    blocked_by TEXT DEFAULT "",
    priority INTEGER DEFAULT 2,
    deps TEXT DEFAULT "",
    due TEXT DEFAULT "",
    created TEXT DEFAULT "",
    updated TEXT DEFAULT "",
    done_at TEXT DEFAULT "",
    created_by TEXT DEFAULT ""
);
-- M3 信号（2026-10-12）：agent 间直接消息 + 已读回执。to_agent 空=广播。
-- 已读按收件人逐人记（signal_reads），广播才有「全员回执」语义；signals.status 是
-- 冗余的汇总态（unread→read，供列表一眼看），权威在 signal_reads。
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT "",
    from_agent TEXT DEFAULT "",
    to_agent TEXT DEFAULT "",
    project TEXT DEFAULT "",
    kind TEXT DEFAULT "msg",
    content TEXT DEFAULT "",
    status TEXT DEFAULT "unread",
    read_at TEXT DEFAULT "",
    read_by TEXT DEFAULT ""
);
CREATE TABLE IF NOT EXISTS signal_reads (
    signal_id INTEGER,
    agent TEXT,
    read_at TEXT DEFAULT "",
    PRIMARY KEY (signal_id, agent)
);
-- M11 决策回路（2026-10-12）：决策记录写了没人回顾=白写。写决策时挂 review_date，
-- 到期进「待验证」队列（心跳开工推送第三段），回顾时记 verdict（right/wrong/partial）。
-- AgDR 决策本身还是记录尾段（不重构），本表只挂「到期-回顾」闭环。
CREATE TABLE IF NOT EXISTS decision_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER,
    review_date TEXT DEFAULT "",
    status TEXT DEFAULT "pending",
    verdict TEXT DEFAULT "",
    note TEXT DEFAULT "",
    agent TEXT DEFAULT "",
    created TEXT DEFAULT "",
    reviewed_at TEXT DEFAULT ""
);
-- M9 视角（2026-10-12）：agent 关注清单（项目/标签）。多 agent 场景下每个 agent 只关心
-- 自己的地盘——关注项目有进展时心跳附加一段速览，不开工在那个项目也知道。
CREATE TABLE IF NOT EXISTS agent_views (
    agent TEXT,
    scope_type TEXT DEFAULT "project",
    scope_value TEXT DEFAULT "",
    created TEXT DEFAULT "",
    PRIMARY KEY (agent, scope_type, scope_value)
);
-- M4 检查点（2026-10-12）：任务级断点续跑。长任务/跨会话任务定期存进展快照，
-- 崩溃、换人接手、隔天续跑时不用从零翻记录。kind：manual=手写 /
-- auto_claim=认领时自动存的开工资锚点（断点续跑的第一块砖）。
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    agent TEXT DEFAULT "",
    ts TEXT DEFAULT "",
    kind TEXT DEFAULT "manual",
    content TEXT DEFAULT ""
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT "",
    agent TEXT DEFAULT "",
    project TEXT DEFAULT "",
    kind TEXT DEFAULT "progress",
    content TEXT DEFAULT "",
    ref TEXT DEFAULT ""
);
CREATE TABLE IF NOT EXISTS distill_seen (
    record_id INTEGER PRIMARY KEY,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    agent TEXT DEFAULT "",
    done TEXT DEFAULT "",
    next_step TEXT DEFAULT "",
    excluded TEXT DEFAULT "",
    status TEXT DEFAULT "open",
    claimed_by TEXT DEFAULT "",
    claimed_at TEXT DEFAULT "",
    created TEXT DEFAULT ""
);
CREATE TABLE IF NOT EXISTS files (
    project TEXT, path TEXT, name TEXT, grp TEXT,
    size INTEGER DEFAULT 0, mtime REAL DEFAULT 0,
    PRIMARY KEY (project, path)
);
-- 技能原生化（阶段1，2026-10-11）：技能从「agent 侧全量描述常驻」改为「大脑索引按需召回」。
-- 只存元数据（名字/一句话说明/在哪），文件实体仍在各 agent 目录，不动不删。
CREATE TABLE IF NOT EXISTS skills (
    name TEXT DEFAULT "",
    agent TEXT DEFAULT "",
    path TEXT DEFAULT "",
    desc TEXT DEFAULT "",
    hits INTEGER DEFAULT 0,
    last_hit TEXT DEFAULT "",
    PRIMARY KEY (agent, path)
);
-- 软件资产台账（技能原生化·阶段5，L3 数据层）：不是技能，是「本机/可用软件」索引。
-- agent 要装工具前先查这里，避免重复下载与山寨源；首批来自 tools.accat.top 的 21 款。
CREATE TABLE IF NOT EXISTS software (
    name TEXT DEFAULT "",
    category TEXT DEFAULT "",
    license TEXT DEFAULT "",
    website TEXT DEFAULT "",
    desc TEXT DEFAULT "",
    source TEXT DEFAULT "",
    version TEXT DEFAULT "",
    PRIMARY KEY (name)
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    source TEXT DEFAULT "human",
    content TEXT NOT NULL,
    tags TEXT DEFAULT "",
    link TEXT DEFAULT "",
    mood TEXT DEFAULT "",
    starred INTEGER DEFAULT 0,
    open_at TEXT DEFAULT "",
    context TEXT DEFAULT ""
);
-- Y1 编排层：把「每次都要手工重走的流程」固化成可执行的 playbook。
-- playbooks=流程定义（builtin=1 为内置模板，同名 upsert 只改自定义）；
-- playbook_steps=步骤（action 指向 ACTIONS 注册表，manual=需人确认）；
-- playbook_runs=一次执行（cursor 指向下一个待跑 seq，失败即停可 resume）；
-- playbook_run_steps=每步的执行结果（断点续跑的事实依据，不重写历史）。
CREATE TABLE IF NOT EXISTS playbooks (
    name TEXT PRIMARY KEY,
    title TEXT DEFAULT "",
    desc TEXT DEFAULT "",
    builtin INTEGER DEFAULT 0,
    created TEXT DEFAULT "",
    updated TEXT DEFAULT ""
);
CREATE TABLE IF NOT EXISTS playbook_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playbook TEXT NOT NULL,
    seq INTEGER DEFAULT 0,
    key TEXT DEFAULT "",
    title TEXT DEFAULT "",
    action TEXT DEFAULT "manual",
    args TEXT DEFAULT "",
    note TEXT DEFAULT ""
);
CREATE INDEX IF NOT EXISTS idx_pbstep_pb ON playbook_steps(playbook, seq);
CREATE TABLE IF NOT EXISTS playbook_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playbook TEXT NOT NULL,
    project TEXT DEFAULT "",
    agent TEXT DEFAULT "",
    status TEXT DEFAULT "running",
    started TEXT DEFAULT "",
    finished TEXT DEFAULT "",
    cursor INTEGER DEFAULT 0,
    summary TEXT DEFAULT ""
);
CREATE INDEX IF NOT EXISTS idx_pbrun_pb ON playbook_runs(playbook);
CREATE TABLE IF NOT EXISTS playbook_run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    seq INTEGER DEFAULT 0,
    key TEXT DEFAULT "",
    title TEXT DEFAULT "",
    action TEXT DEFAULT "",
    status TEXT DEFAULT "pending",
    output TEXT DEFAULT "",
    finished TEXT DEFAULT ""
);
CREATE INDEX IF NOT EXISTS idx_pbrunstep_run ON playbook_run_steps(run_id, seq);
-- Y2 自动化：事件驱动。enabled 默认 0——触发器是大脑的**主动行为**，
-- 配错就是刷屏/误动作，宁可让人显式开，不可默认替人做主。
CREATE TABLE IF NOT EXISTS triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT DEFAULT "",
    on_event TEXT DEFAULT "heartbeat",
    condition TEXT DEFAULT "",
    action_type TEXT DEFAULT "notify",
    action_args TEXT DEFAULT "",
    enabled INTEGER DEFAULT 0,
    cooldown_min INTEGER DEFAULT 720,
    last_fired TEXT DEFAULT "",
    fire_count INTEGER DEFAULT 0,
    agent TEXT DEFAULT "",
    created TEXT DEFAULT ""
);
CREATE INDEX IF NOT EXISTS idx_trig_event ON triggers(on_event, enabled);
"""


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# user=GUI 本机操作、unknown/migrated=历史数据来源，不算 agent 身份
_NON_AGENT = {"", "user", "unknown", "migrated"}


def _norm_agent(agent: str) -> str:
    """agent 身份统一小写：ZCode/zcode 双身份曾并存，过滤/统计/徽章处处错位
    （2026-10-01 时间线过滤滤光记录事故根因）。"""
    return (agent or "").strip().lower()


def check_agent(agent: str, root: str = "") -> tuple[str, str]:
    """校验 agent 参数是否在注册表内。返回 (规范 id 或 "", 错误或 "")。

    2026-10-09 新增：agent 名此前是自由文本，填错（填成别家/自造名）无人拦，
    直接污染时间线归属、过滤与统计。改为**当场拦下**并给出可用名单。

    宽容处理：
    - 空/unknown/migrated/user → 放行（这些是"无主"语义，不是错误身份）
    - 显示名与 id 混填（"Claude Code"/"WorkBuddy"）→ 归一到 id，不报错
    - 不在册的名字 → 报错（不静默改写，错了要让人知道）
    """
    raw = (agent or "").strip()
    low = _norm_agent(raw)
    if low in _NON_AGENT:
        return low, ""
    reg = core.agent_known_names(root)
    key = core.agent_key(raw)
    if key in reg:
        return key, ""
    if low in reg:  # agent_key可能把带空格名拆过头，这里兜一层原样小写
        return low, ""
    avail = "、".join(sorted(reg))
    return "", (f"agent 名「{raw}」不在注册表内，已拒收（避免时间线归属/统计出错）。"
                f"可用：{avail}；确实无法归属时传 unknown。")


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


SCHEMA_VERSION = 1
# B11 schema 版本号。约定：新增表/列/迁移时 **+1**，并在本注释里记一笔：
#   v1 = 基线（本登记落地的时刻；此前所有迁移都靠幂等的 _migrate_columns 逐列 PRAGMA 试探，
#        能补列但没有版本号——无法回答「这个库现在是哪个版本」「是不是降级运行了」）。
# 故意**不**替换 _migrate_columns：它已幂等且被大量回归覆盖，推翻重来风险远大于收益。
# 这里只补缺口的一环——版本登记 + 降级检测。

_SCHEMA_VER_SYNCED: set = set()  # 进程内只登记一次的 root 集合（ensure_schema 每请求都被调用，不能每次查库）


def schema_version(root: str) -> dict:
    """B11 只读：本库的 schema 版本状态。
    返回 {"version": 代码版本, "db": 库记录版本(未登记为 None), "drift": "downgrade"/""}。
    drift='downgrade' = 库版本高于代码版本 → **旧代码跑新库**（会读错列/漏新表），必须能举证。"""
    try:
        with db_conn(root) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except Exception:  # noqa: BLE001  表还没建/库不可读：只读观测不得抛
        return {"version": SCHEMA_VERSION, "db": None, "drift": ""}
    raw = row["value"] if row else None
    db_v = int(raw) if raw is not None and str(raw).lstrip("-").isdigit() else None
    drift = "downgrade" if (db_v is not None and db_v > SCHEMA_VERSION) else ""
    return {"version": SCHEMA_VERSION, "db": db_v, "drift": drift}


def _sync_schema_version(root: str) -> None:
    """B11：把当前 SCHEMA_VERSION 登记进 meta。契约：
    ① 幂等——已登记且同版本则不动；② 降级运行（库 > 代码）**保留库里的高版本不覆盖**，
       让 schema_version() 能持续报 downgrade（否则一次就把证据抹了）；
    ③ 绝不抛异常——版本登记失败不影响建表/补列主链路。"""
    if root in _SCHEMA_VER_SYNCED:      # 进程内热路径短路（R2 后 ensure_schema 每请求都跑）
        return
    try:
        st = schema_version(root)
        if st["drift"] == "downgrade" or st["db"] == SCHEMA_VERSION:
            return
        with db_conn(root) as conn:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                         ("schema_version", str(SCHEMA_VERSION)))
    except Exception:  # noqa: BLE001
        pass
    finally:
        _SCHEMA_VER_SYNCED.add(root)


def ensure_schema(root: str) -> str:
    """仅建表 + 增量补列（毫秒级，不跑文件迁移）：MCP server 启动/根目录切换时调用。
    防"代码已部署但真实库缺新表/新列"——v2.5.2 的 distill_seen 上轮只在测试库验证过，
    真实库无任何新进程跑过 init_db，hub_distill 直接 no such table（2026-10-02 实锤）；
    v2.10.0 的 salience 列再实锤一次（serve 只建表不补列，部署版反馈通道直接
    no such column，2026-10-04 协议级实测抓到）。补列/回填均幂等且毫秒级。
    返回错误或 ""。根目录不存在时报错（与 init_db 一致，不凭空建目录）。
    Y1：末尾补种内置 playbook 模板——真实库建表后必须有模板，否则 hub_playbook 空手而回。"""
    if not root or not os.path.isdir(root):
        return "根目录不存在"
    try:
        db_path(root).parent.mkdir(parents=True, exist_ok=True)
        with db_conn(root) as conn:
            conn.executescript(SCHEMA)
        _migrate_columns(root)
        _backfill_tag_schema(root)
        _pb_seed(root)  # 只补缺失 + 进程内只查一次（ensure_schema 被高频调用，不能每次扫库）
        _sync_schema_version(root)  # B11：登记版本（同样进程内一次；失败静默，不影响建表）
        return ""
    except sqlite3.Error as e:
        return f"大脑 schema 初始化失败：{e}"


def init_db(root: str) -> str:
    """建表 + 增量迁移（幂等，每次启动跑，已有数据不重复导入）。返回错误或 ""。"""
    if not root or not os.path.isdir(root):
        return "根目录不存在"
    try:
        err = ensure_schema(root)
        if err:
            return err
        _migrate_records(root)
        _migrate_memory(root)
        _migrate_jsonl(root)
        _backfill_agents(root)
        _normalize_agents(root)
        _migrate_columns(root)
        _backfill_tag_schema(root)
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
        # ★ 去重键 = (project, source, title, content[:256])，**不含 agent、也不含 date**（2026-10-10 修）：
        #   · 不含 agent：agent 是派生值，会随解析规则改进变化（白名单补全/fallback 调整），
        #     放进去会让"同一段文字在两套规则下算出不同 agent"被误判为新记录、整批重插。
        #   · 不含 date：无段内日期的条目用**文件 mtime** 兜底，而 agent 每次追加记录都会改 mtime，
        #     于是这些条目会反复被判定为新增（实测：追加一段后重跑，105→164 条）。
        #   · 用 source（migrated:<文件>:<行号>）作稳定身份——追加到文件末尾不影响既有行号。
        #   ⚠ 改这里必须同步改下方 key 的构造，两者结构必须完全一致（上一版只改此处漏改对方，
        #     导致键结构不匹配、去重彻底失效、每次 init_db 全量重插 451 条）。
        known = {(r["project"], r["source"] or "", r["title"], (r["content"] or "")[:256])
                 for r in conn.execute("SELECT project,source,title,content FROM records")}
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
            # 目录前缀推断不出就留空——**不猜 zcode**（2026-10-09：旧代码兜底 "zcode"，
            # 让 858 条无主记录被错误归到 zcode 名下，时间线按 agent 过滤/统计随之出错）。
            # 空值入库即 unknown，_NON_AGENT 已含它→ 不会污染 agents 表统计。
            fallback = pm.group(1).lower() if pm else ""
            entries = core.parse_record(core.read_text(src), fallback_agent=fallback,
                                        fallback_date=core._mtime_date(src),
                                        known=core.agent_known_names(root))
            if not entries and src.name != core.RECORD_NAME:  # 主文档整篇无段头 → 文件级一条
                entries = [core.RecordEntry(date=core._mtime_date(src), agent=fallback,
                                            agent_raw="", title=f"[主文档] {src.name}", line_no=0,
                                            body=core.read_text(src)[:4000])]
            for r in entries:
                # ★ key 必须与上面 known 的元素**同结构**（2026-10-10 二次修复）：
                #   known = (project, source, title, content[:256])，本处同构。
                #   上一版只改了 known、没改这里，两套键永不相等 → 去重彻底失效、
                #   每次 init_db 全量重插（实测临时库两次跑 210→420）。
                key = (entry.name, f"migrated:{src.name}:{r.line_no}", r.title,
                       (r.body or "")[:256])
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


def _backfill_tag_schema(root: str) -> None:
    """P1-2：用现存记忆标签一次性初始化 tag_schema 注册表（meta 标记防重跑，幂等）。
    大小写不敏感聚合；显示形态取该标签首次出现的写法；use_count=携带该标签的记忆条数。"""
    with db_conn(root) as conn:
        if conn.execute("SELECT value FROM meta WHERE key='tag_schema_backfill'").fetchone():
            return
        canon: dict = {}
        counts: dict = {}
        for r in conn.execute("SELECT tags FROM memories WHERE tags<>'' ORDER BY id"):
            seen = set()
            for raw in re.split(r"[,，]", r["tags"] or ""):
                t = _norm_tag_one(raw)
                if not t or len(t) > TAG_MAX:
                    continue
                key = t.lower()
                canon.setdefault(key, t)
                if key not in seen:
                    seen.add(key)
                    counts[key] = counts.get(key, 0) + 1
        for key, t in canon.items():
            conn.execute("INSERT OR IGNORE INTO tag_schema(tag, use_count, first_seen, last_seen) "
                         "VALUES(?,?,?,?)", (t, counts.get(key, 0), "", ""))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('tag_schema_backfill','1')")


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
        if "salience" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN salience REAL DEFAULT 1.0")
        # v2.19 时效治理四列（2026-10-09）：記憶失效的基本面
        # superseded_by：被哪条记忆取代（0=现行）；valid_until：有效期止（''=长期）
        # confidence：置信度 0~1；evidence：证据链 "r:12,r:34,m:7"（records/memories id）
        for col, ddl in (("superseded_by", "INTEGER DEFAULT 0"),
                         ("valid_until", "TEXT DEFAULT ''"),
                         ("confidence", "REAL DEFAULT 1.0"),
                         ("evidence", "TEXT DEFAULT ''")):
            if col not in cols:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {ddl}")
        # M6 成熟度列（2026-10-12）：存量按历史使用回填——pinned 或 use>=CORE_AT → core；
        # use>=VALIDATE_AT → validated；其余 draft。只此一次（补列后才跑），幂等靠列存在判断。
        if "maturity" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN maturity TEXT DEFAULT 'draft'")
            conn.execute(
                "UPDATE memories SET maturity=CASE WHEN pinned=1 AND use_count>=? THEN 'core' "
                "WHEN use_count>=? THEN 'validated' ELSE 'draft' END",
                (CORE_AT, VALIDATE_AT))
        # P1-1（2026-10-11）records 双时相版本列：valid_from/valid_until/superseded_by
        # 「失效 ≠ 删除」——旧版本行保留可追溯，检索只认现行有效（_rec_live_sql）。
        rcols = {r["name"] for r in conn.execute("PRAGMA table_info(records)")}
        for col, ddl in (("valid_from", "TEXT DEFAULT ''"),
                         ("valid_until", "TEXT DEFAULT ''"),
                         ("superseded_by", "INTEGER DEFAULT 0")):
            if col not in rcols:
                conn.execute(f"ALTER TABLE records ADD COLUMN {col} {ddl}")
        if "valid_from" not in rcols:  # 首次迁移才回填（幂等）：老数据取 date，无 date 回退 created 前 10 位
            conn.execute("UPDATE records SET valid_from="
                         "COALESCE(NULLIF(date,''), substr(created,1,10)) "
                         "WHERE valid_from IS NULL OR valid_from=''")
        # P1-2（2026-10-11）记录类型收口：老数据回填（含决策段→decision，其余→work_log）
        if "record_type" not in rcols:
            conn.execute("ALTER TABLE records ADD COLUMN record_type TEXT DEFAULT ''")
            conn.execute("UPDATE records SET record_type=CASE WHEN content LIKE '%## 决策记录%' "
                         "THEN 'decision' ELSE 'work_log' END "
                         "WHERE record_type IS NULL OR record_type=''")
        # M1（2026-10-12）任务工作图：priority(0急~3低,默认2) / deps(前置任务 id,逗号分隔) / due(截止日)
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        for col, ddl in (("priority", "INTEGER DEFAULT 2"),
                         ("deps", "TEXT DEFAULT ''"),
                         ("due", "TEXT DEFAULT ''")):
            if col not in tcols:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
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
        # 归属回填（2026-10-12）：projects.agent 从未写入过（真实库 138/138 全空 → 一律显示「未知」）。
        # 从该项目最近一条有署名的记录取 agent；幂等，仅补空值。
        conn.execute(
            "UPDATE projects SET agent=(SELECT r.agent FROM records r "
            "WHERE r.project=projects.name AND r.agent!='' ORDER BY r.id DESC LIMIT 1) "
            "WHERE (agent='' OR agent IS NULL) AND EXISTS("
            "SELECT 1 FROM records r WHERE r.project=projects.name AND r.agent!='')")
        # agents.last_seen 回填（2026-10-12）：初版 _backfill_agents 只写了 first_seen/records，
        # last_seen 留空 → claude/hermes 等历史 agent「最后活跃时间」未知，导致「谁静默了」不可判定
        # （同类盲区：字段只在 INSERT 里加、没有存量回填路径）。从 records 的 MAX(date) 补；幂等，仅补空值。
        conn.execute(
            "UPDATE agents SET last_seen=(SELECT MAX(r.date) FROM records r "
            "WHERE r.agent=agents.name AND r.date!='') "
            "WHERE (last_seen='' OR last_seen IS NULL) AND EXISTS("
            "SELECT 1 FROM records r WHERE r.agent=agents.name AND r.date!='')")
        # 无记录可溯源的历史项目（多为已归档的 old-style 目录）——目录名前缀即归属：
        # 「hermes - 网络切换脚本」→ hermes。与 core 迁移期的归属判定一致。
        conn.execute(
            "UPDATE projects SET agent=lower(trim(substr(name,1,instr(name,' - ')-1))) "
            "WHERE (agent='' OR agent IS NULL) AND instr(name,' - ')>1")
        # 保留目录（00_Inbox/_archive/…）若被历史版本误登记成项目，清掉（幂等）
        conn.execute(
            f"DELETE FROM projects WHERE name IN ({','.join('?' * len(core.RESERVED))})",
            tuple(core.RESERVED))
        # notes 创意层三列（2026-10-07 批 A）：收藏重温 / 时光胶囊开启日 / 自动上下文
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
        if "starred" not in cols:
            conn.execute("ALTER TABLE notes ADD COLUMN starred INTEGER DEFAULT 0")
        if "open_at" not in cols:
            conn.execute("ALTER TABLE notes ADD COLUMN open_at TEXT DEFAULT ''")
        if "context" not in cols:
            conn.execute("ALTER TABLE notes ADD COLUMN context TEXT DEFAULT ''")


# ---------------------------------------------------------------- 工作记录

def render_decision(context: str = "", options: str = "", decision: str = "",
                    tradeoffs: str = "") -> str:
    """3.2 AgDR：决策字段渲染成记录尾部「## 决策记录」段（字段名对齐 AgDR：
    上下文/权衡过的选项/决策/接受的取舍——记录"为什么"而非"做了什么"）。
    全空返回 ""（不加段，老调用零影响）；半缺合法（如只有上下文没有决策）。
    多行值缩进续行，保持 markdown 可读；渲染进 content 即被搜索/存档/outline 全链路覆盖。"""
    fields = [("上下文", context), ("权衡过的选项", options),
              ("决策", decision), ("接受的取舍", tradeoffs)]
    rows = []
    for name, val in fields:
        vl = [ln.strip() for ln in str(val or "").splitlines() if ln.strip()]
        if not vl:
            continue
        rows.append(f"- {name}：{vl[0]}")
        rows.extend(f"  {ln}" for ln in vl[1:])
    if not rows:
        return ""
    return "\n\n## 决策记录\n" + "\n".join(rows)


# ---------------------------------------------------------------- P1-2 schema 收口（类型枚举 + 标签规约）

TAG_MAX = 32     # 单个标签长度上限（超长丢弃——多为误把整句当标签）
TAG_CAP = 12     # 单条记忆标签数上限


def _infer_record_type(content: str, explicit: str = "") -> str:
    """记录类型校验/推断：显式合法值优先；否则按内容推断（含「## 决策记录」→ decision，
    其余 → work_log）。非法显式值不硬拒（共享库上因拼写拒写得不偿失）——MCP 层负责拒收提示。"""
    rt = (explicit or "").strip()
    if rt in RECORD_TYPES:
        return rt
    return "decision" if "## 决策记录" in (content or "") else "work_log"


def _norm_tag_one(raw: str) -> str:
    """单个标签规约：去首尾空白、内部空白折叠为单空格。"""
    return re.sub(r"\s+", " ", (raw or "").strip())


def _norm_tags(tags: str) -> str:
    """标签串规约（写入即校验的温和版）：全/半角逗号切分，逐项规约、去空、去重（大小写不敏感）、
    超长丢弃、条数封顶。不硬拒未知标签——收口靠「规约 + 注册表」而非拒绝（见 _register_tags）。"""
    if not tags:
        return ""
    out, seen = [], set()
    for raw in re.split(r"[,，]", str(tags)):
        t = _norm_tag_one(raw)
        if not t or len(t) > TAG_MAX:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return ",".join(out[:TAG_CAP])


def _register_tags(conn, tags: str) -> str:
    """把标签注册进 tag_schema（use_count+1、落 last_seen），大小写变体归一到已注册的显示形态，
    返回按注册表归一后的标签串（调用方存这个）。fail-open：注册失败照常返回原串（不阻断写入）。"""
    if not tags:
        return tags
    now = _now()
    try:
        fixed = []
        for t in tags.split(","):
            if not t:
                continue
            row = conn.execute("SELECT tag FROM tag_schema WHERE lower(tag)=lower(?)",
                               (t,)).fetchone()
            if row:
                conn.execute("UPDATE tag_schema SET use_count=use_count+1, last_seen=? WHERE tag=?",
                             (now, row["tag"]))
                fixed.append(row["tag"])
            else:
                conn.execute("INSERT OR IGNORE INTO tag_schema(tag, use_count, first_seen, last_seen) "
                             "VALUES(?,1,?,?)", (t, now, now))
                fixed.append(t)
        return ",".join(fixed)
    except Exception:
        return tags


def tag_list(root: str, limit: int = 100) -> list:
    """标签注册表（使用次数倒序）——标签收口的可观测面（供体检/界面/统计）。"""
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT tag, use_count, first_seen, last_seen FROM tag_schema "
            "ORDER BY use_count DESC, tag LIMIT ?", (max(1, min(int(limit), 500)),))]


# ---------------------------------------------------------------- P1-3 关系 + 属性继承（双链/引用图底座）

_REL_TO_KINDS = {"r": ("record", "records"), "m": ("memory", "memories"), "n": ("note", "notes")}
REL_LINK_RE = re.compile(r"\[\[([^\[\]]{1,80})\]\]")


def _link_targets(content: str) -> list:
    """抽取正文 [[X]] 双链目标（去重保序）。[[r:12]]/[[m:76]]/[[n:3]] = 显式实体引用，其余按项目名。"""
    out, seen = [], set()
    for m in REL_LINK_RE.finditer(content or ""):
        t = m.group(1).strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _resolve_link(conn, target: str):
    """[[X]] → (to_kind, to_ref)。显式实体引用需存在（不存在返回 None）；其余一律按项目名
    （前向兼容：项目尚未出现也存，等它出现反向链接即生效）。"""
    m = re.match(r"^([rmn]):(\d+)$", target)
    if m:
        kind, tbl = _REL_TO_KINDS[m.group(1)]
        if not conn.execute(f"SELECT 1 FROM {tbl} WHERE id=?", (int(m.group(2)),)).fetchone():
            return None
        return (kind, m.group(2))
    return ("project", target)


def sync_links(conn, from_kind: str, from_id: int, content: str) -> int:
    """P1-3：重抽 [[双链]] → relations 的 mention 边（先删本源旧边再插，幂等）。返回边数。fail-open。"""
    try:
        conn.execute("DELETE FROM relations WHERE from_kind=? AND from_id=? AND kind='mention'",
                     (from_kind, from_id))
        n = 0
        for t in _link_targets(content):
            r = _resolve_link(conn, t)
            if not r:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO relations(from_kind,from_id,to_kind,to_ref,kind,created) "
                "VALUES(?,?,?,?,'mention',?)", (from_kind, int(from_id), r[0], r[1], _now()))
            n += 1
        return n
    except Exception:
        return 0


def relation_add(root: str, from_kind: str, from_id: int, to_kind: str, to_ref: str,
                 kind: str = "link") -> str:
    """手工建一条边（kind 默认 link；[[双链]] 自动边用 mention）。"""
    with db_conn(root) as conn:
        conn.execute("INSERT OR IGNORE INTO relations(from_kind,from_id,to_kind,to_ref,kind,created) "
                     "VALUES(?,?,?,?,?,?)",
                     (from_kind, int(from_id), to_kind, str(to_ref), kind, _now()))
    return ""


def relation_remove(root: str, rel_id: int) -> str:
    with db_conn(root) as conn:
        cur = conn.execute("DELETE FROM relations WHERE id=?", (int(rel_id),))
        if cur.rowcount == 0:
            return f"关系 #{rel_id} 不存在"
    return ""


def relation_list(root: str, from_kind: str = "", from_id: int = 0, to_kind: str = "",
                  to_ref: str = "", limit: int = 100) -> list:
    q, args = "SELECT * FROM relations WHERE 1=1", []
    if from_kind:
        q += " AND from_kind=?"; args.append(from_kind)
    if from_id:
        q += " AND from_id=?"; args.append(int(from_id))
    if to_kind:
        q += " AND to_kind=?"; args.append(to_kind)
    if to_ref:
        q += " AND to_ref=?"; args.append(str(to_ref))
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(q, args)]


def _src_snippet(conn, kind: str, sid: int, width: int = 60) -> tuple:
    """来源摘要：(标题或前 N 字, 项目)。找不到（已删）给 ("(已删除)", "")。"""
    if kind == "record":
        r = conn.execute("SELECT title,content,project FROM records WHERE id=?", (sid,)).fetchone()
        return (((r["title"] or r["content"] or "")[:width], r["project"] or "")
                if r else ("(已删除)", ""))
    if kind == "memory":
        r = conn.execute("SELECT content,project FROM memories WHERE id=?", (sid,)).fetchone()
        return ((r["content"] or "")[:width], r["project"] or "") if r else ("(已删除)", "")
    if kind == "note":
        r = conn.execute("SELECT content FROM notes WHERE id=?", (sid,)).fetchone()
        return ((r["content"] or "")[:width] if r else "(已删除)", "")
    return ("", "")


def backlinks(root: str, to_kind: str, to_ref: str, limit: int = 50) -> list:
    """谁引用了我（引用图/反链数据源）：连来源摘要一起返回。"""
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM relations WHERE to_kind=? AND to_ref=? ORDER BY id DESC LIMIT ?",
            (to_kind, str(to_ref), max(1, min(int(limit), 500))))]
        for r in rows:
            r["snippet"], r["project"] = _src_snippet(conn, r["from_kind"], r["from_id"])
        return rows


def attr_set(root: str, node_kind: str, node_ref: str, key: str, value: str,
             inheritable: bool = False) -> str:
    """设属性（UPSERT）。project 上 inheritable=1 会被其下 record/memory 继承。返回错误或 ""。"""
    key = (key or "").strip()
    if not key:
        return "key 不能为空"
    if node_kind not in ("project", "record", "memory", "note"):
        return f"node_kind 非法：{node_kind!r}"
    with db_conn(root) as conn:
        conn.execute(
            "INSERT INTO attrs(node_kind,node_ref,key,value,is_inheritable,created) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(node_kind,node_ref,key) DO UPDATE SET "
            "value=excluded.value, is_inheritable=excluded.is_inheritable",
            (node_kind, str(node_ref), key, str(value or ""), 1 if inheritable else 0, _now()))
    return ""


def attr_delete(root: str, node_kind: str, node_ref: str, key: str) -> str:
    with db_conn(root) as conn:
        cur = conn.execute("DELETE FROM attrs WHERE node_kind=? AND node_ref=? AND key=?",
                           (node_kind, str(node_ref), (key or "").strip()))
        if cur.rowcount == 0:
            return f"属性 {key} 不存在"
    return ""


def _project_of(conn, kind: str, eid: int) -> str:
    tbl = "records" if kind == "record" else ("memories" if kind == "memory" else "")
    if not tbl:
        return ""
    r = conn.execute(f"SELECT project FROM {tbl} WHERE id=?", (eid,)).fetchone()
    return (r["project"] or "") if r else ""


def attr_effective_all(root: str, node_kind: str, node_ref: str) -> list:
    """节点全部生效属性（直接 + 从所属 project 继承；直接覆盖继承）。[{'key','value','from'}]"""
    out: dict = {}
    with db_conn(root) as conn:
        if node_kind in ("record", "memory"):
            try:
                proj = _project_of(conn, node_kind, int(node_ref))
            except (TypeError, ValueError):
                proj = ""
            if proj:
                for r in conn.execute("SELECT key,value FROM attrs WHERE node_kind='project' "
                                      "AND node_ref=? AND is_inheritable=1", (proj,)):
                    out[r["key"]] = {"key": r["key"], "value": r["value"], "from": "inherited"}
        for r in conn.execute("SELECT key,value FROM attrs WHERE node_kind=? AND node_ref=?",
                              (node_kind, str(node_ref))):
            out[r["key"]] = {"key": r["key"], "value": r["value"], "from": "direct"}
    return sorted(out.values(), key=lambda x: x["key"])


def attr_effective(root: str, node_kind: str, node_ref: str, key: str) -> str:
    """生效属性：直接优先；record/memory 未直设时从所属 project 继承（仅 is_inheritable=1）。"""
    with db_conn(root) as conn:
        r = conn.execute("SELECT value FROM attrs WHERE node_kind=? AND node_ref=? AND key=?",
                         (node_kind, str(node_ref), key)).fetchone()
        if r is not None:
            return r["value"]
        if node_kind in ("record", "memory"):
            try:
                proj = _project_of(conn, node_kind, int(node_ref))
            except (TypeError, ValueError):
                proj = ""
            if proj:
                r2 = conn.execute("SELECT value FROM attrs WHERE node_kind='project' AND node_ref=? "
                                  "AND key=? AND is_inheritable=1", (proj, key)).fetchone()
                if r2 is not None:
                    return r2["value"]
    return ""


def add_record(root: str, project: str, agent: str, date: str, title: str, content: str,
               source: str = "hub", context: str = "", options: str = "",
               decision: str = "", tradeoffs: str = "", record_type: str = "",
               review_date: str = "") -> int:
    """写一条工作记录（hub 入口）。自动登记项目（历史目录迁移时已登记），并刷新项目活跃状态。
    source：hub=人工经 MCP 写入；auto=会话边界 hook 自动捕获（未经蒸馏，2.1）。
    决策字段（3.2 AgDR，可选）：context/options/decision/tradeoffs 传任意非空值即在
    正文尾部追加「## 决策记录」段；全不传时行为与旧版完全一致。
    record_type（P1-2，可选）：work_log/decision/todo/note；缺省按内容推断（含决策段→decision）。
    review_date（M11，可选）：YYYY-MM-DD——到期后此决策进「待验证」队列（心跳推送+回顾工具），
    传了才建（不传不建，零行为变化）。"""
    source = source if source in ("hub", "auto") else "hub"
    content = content + render_decision(context, options, decision, tradeoffs)
    rtype = _infer_record_type(content, record_type)
    _ensure_emb()  # 模型懒加载移出写事务：避免 12s 加载期间持有 WAL 写锁拖垮并发写
    with db_conn(root) as conn:
        # 项目归属随首条记录落地（2026-10-12）：此前只写 name/created，projects.agent 恒空，
        # 导致 hub_get_project/GUI 一律显示「归属:未知」（真实库实测 138/138 全空）。
        agent = _norm_agent(agent)
        conn.execute("INSERT OR IGNORE INTO projects(name, created, agent) VALUES(?,?,?)",
                     (project, _now(), agent))
        _upsert_agent(conn, agent, project, record=True)
        cur = conn.execute(
            "INSERT INTO records(project,agent,date,title,content,source,created,valid_from,record_type) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (project, agent, date, title, content, source, _now(), (date or _now()[:10]), rtype))
        conn.execute("UPDATE projects SET updated=?, status='active', "
                     "agent=CASE WHEN agent='' OR agent IS NULL THEN ? ELSE agent END "
                     "WHERE name=? AND status!='archived'", (date or _now()[:10], agent, project))
        sync_links(conn, "record", cur.lastrowid, content)  # P1-3：正文 [[双链]] → relations
        _ensure_emb_for(root, "records", cur.lastrowid, title, content, project, conn=conn)
        # M11：带 review_date 的决策记录挂「待验证」——同一事务同连接（不嵌套开新连接，
        # 避开 M4 实测的写锁坑）；不传 review_date 行为与旧版完全一致。
        rd = (review_date or "").strip()[:10]
        if rd:
            conn.execute(
                "INSERT INTO decision_reviews(record_id, review_date, agent, created) "
                "VALUES(?,?,?,?)",
                (cur.lastrowid, rd, _norm_agent(agent) or "", _now()))
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
        conn.execute("UPDATE records SET status='deleted', valid_until=? WHERE id=?",
                     (_now()[:10], row["id"]))
        return "", f"#{row['id']} {row['date']} {row['project']}：{row['title']}"


def undo_record(root: str, rec_id) -> tuple:
    """软删**指定 id** 的 hub 记录（非栈式）。返回 (错误或"", 撤掉的记录摘要)。

    为什么要有它（Round 4 / 2026-10-14）：GUI 流水页让用户「选中一条流水再撤销」，
    但此前调的是栈式的 `undo_last_record(agent)` —— 撤的永远是该 agent **最新一条**。
    实测：选中较早的 #2、点撤销，被软删的是 #3（操作错对象），且没有任何提示。
    界面承诺撤销"选中项"，就必须按 id 撤。
    口径与 `undo_last_record` 一致：仅 hub 写入（source='hub'）且仍 active 的记录可撤。
    """
    try:
        rid = int(rec_id)
    except (TypeError, ValueError):
        return "记录 id 必须是整数", ""
    with db_conn(root) as conn:
        row = conn.execute(
            "SELECT id,project,date,title FROM records "
            "WHERE id=? AND source='hub' AND status='active'", (rid,)).fetchone()
        if not row:
            return f"#{rid} 不是可撤销的 hub 记录（可能已被撤销，或不是 agent 写入的）", ""
        conn.execute("UPDATE records SET status='deleted', valid_until=? WHERE id=?",
                     (_now()[:10], rid))
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


def records_count(root: str, project: str = "", agent: str = "") -> int:
    """records 现行有效行数（P2-3 虚拟滚动：阈值判定 + 分页 total）。"""
    q = "SELECT COUNT(*) FROM records WHERE status='active'"
    args: list = []
    if project:
        q += " AND project=?"
        args.append(project)
    if agent:
        q += " AND agent=?"
        args.append(agent)
    with db_conn(root) as conn:
        return int(conn.execute(q, args).fetchone()[0])


def records_page(root: str, offset: int = 0, limit: int = 100,
                 project: str = "", agent: str = "") -> tuple:
    """records 分页切片（P2-3 虚拟滚动数据源）：SQL LIMIT/OFFSET → (rows, total)。
    排序口径与 list_records 完全一致（date DESC, id DESC）——翻页不重不漏。"""
    where = "status='active'"
    args: list = []
    if project:
        where += " AND project=?"
        args.append(project)
    if agent:
        where += " AND agent=?"
        args.append(agent)
    off = max(0, int(offset))
    lim = max(0, min(int(limit), 500))
    with db_conn(root) as conn:
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM records WHERE {where}", args).fetchone()[0])
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM records WHERE {where} ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
            (*args, lim, off))]
    return rows, total


def records_agents(root: str) -> list:
    """records 出现过的 agent（小写去重；虚拟滚动模式填过滤器下拉用）。"""
    with db_conn(root) as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT lower(agent) FROM records "
            "WHERE status='active' AND agent<>'' ORDER BY 1")]


def _like_escape(w: str) -> str:
    """LIKE 通配符转义：查询词里的 % _ 按字面匹配（否则 query='%' 全库命中，"%"
    在 Windows 通配习惯/SQL 注入探测里都会出现）。配套 SQL 里 ESCAPE '\\'。"""
    return w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_CJK_RE = re.compile(r"[一-鿿]{4,}")


def _retry_bigrams(query: str) -> list:
    """连续中文长串（≥4 字）的相邻 2-gram：中文无空格分词，整串 LIKE 是全或无
    （"大迭代优化"匹配不到只含"迭代优化"的文本）。仅在首轮 0 命中时作重试词表，
    不参与正常路径排序。"""
    out: list = []
    for seg in _CJK_RE.findall(query or ""):
        out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return out[:16]


_CJK_ALL_RE = re.compile(r"[一-鿿]+")  # 任意长度中文段（_retry_bigrams 的 {4,} 是重试语义，这里要全量分词）
_BM25_K1, _BM25_B = 1.5, 0.75  # BM25 经典参数，进常量便于评测集调参（07 计划 P4.2）


def _token_df(root: str, tokens: list) -> dict:
    """query token 在 records+memories 的文档频率（content LIKE 计次，只算活跃行）。

    供覆盖度闸门算 IDF 加权覆盖：常见 bigram（存在/在的）df 高 → idf 低，
    纯计数覆盖治不了「蹭两个常见词冒充强命中」（R4 实测：废话查询靠
    不存+存在+在的 三个常见词凑满覆盖度）。fail-open：读不了库返回 {}，
    闸门退化为纯计数（见 _coverage_gate）。
    """
    df: dict = {}
    if not tokens:
        return df
    try:
        with db_conn(root) as conn:
            for t in tokens:
                like = f"%{_like_escape(t)}%"
                n = 0
                for tbl in ("records", "memories"):
                    n += conn.execute(
                        f"SELECT COUNT(*) FROM {tbl} WHERE content LIKE ? ESCAPE '\\'",
                        (like,)).fetchone()[0]
                df[t] = n
    except Exception:  # noqa: BLE001 闸门是精度优化，读不了库就不加权
        return {}
    return df


def _cjk_run(query: str, text: str) -> int:
    """query 的 CJK 段与 text 的最长公共连续子串长度（<4 返回 0）。

    「大迭代优化」vs 含「迭代优化」的文档 → 4（真短语命中，重试路径的合法场景）；
    「不存在的词」vs「存在的问题」→ 0（只共享 2 字，两个不挨着的常见 bigram 冒充不了）。
    连续 4 字重叠 = 3 个相邻 bigram 全中，是比 idf 更硬的强相关信号。
    """
    for seg in _CJK_ALL_RE.findall(query or ""):
        for L in range(len(seg), 3, -1):   # 从长到短；任一 ≥4 字连续命中即强
            for i in range(len(seg) - L + 1):
                if seg[i:i + L] in text:
                    return L
    return 0


def _latin_garbage_tokens(root: str, tokens: list) -> list:
    """零命中的**拉丁** token（qwerty/zzz/xyzzy）——乱输/拼错的强信号。

    与 CJK 词缺失的区别：「关掉」在库里没有（库用「关闭」）是说法稀缺，不是垃圾；
    而随机拉丁串在任何语料里都不该有。判据：token 纯 ASCII 且两表 df=0。
    fail-open：读不了库返回 []（不闸）。
    """
    df = _token_df(root, tokens)
    return [t for t in tokens if df.get(t, 1) == 0 and t.isascii()]


def _coverage_gate(root: str, rows: list, kw: str, min_match: int = 2) -> list:
    """R4·覆盖度闸门（2026-10-17）：多 token 查询的精度修复——OR 语义保召回，覆盖度保精度。

    治的病（tools/search_matrix.py 实测复现）：OR 语义 + 常见 bigram——查
    「不存在的词qwerty」也能蹭「存在/在的」返回 5 条噪音。外部调研结论
    （agentpatterns：「多词查询 OR 语义…后续 AND 精排或 rerank 会变得必要」）一致。
    规则（满足其一即强候选）：
      ① 语义背书：_semantic_sim ≥ _SEM_MIN_SIM 的 paraphrase 桥——字面词不存在时
         （「关掉错误」→库里的「关闭错误」0.638）只能靠它活；
      ② CJK 连续重叠 ≥4 字（_cjk_run）——「大迭代优化」命中含「迭代优化」的文档，
         重试路径的合法场景，idf 再低也是真短语命中；
      ③ 命中 ≥2 个不同 token 且 IDF 加权质量 ≥50%——常见词（存在/在的）蹭中天然沉底；
      ④ df 读失败时退化为纯计数（命中 ≥2 即强）。
    全弱候选时：查询含拉丁乱码 token（qwerty/zzz，两表零命中）→ 返回空，
    让工具层走「拆词重查」提示；否则打 _weak 标记保留（合法但稀缺，漏比噪贵）。
    query 分词 <3 个 → 原样返回（短查询保持宽容召回）。
    """
    q_tokens = list(dict.fromkeys(_tokenize(kw, max_tokens=16)))
    if len(q_tokens) < 3:
        return rows
    df = _token_df(root, q_tokens)
    idf: dict = {}
    total_mass = 1.0
    if df:
        n_total = 0
        try:
            with db_conn(root) as conn:
                n_total = (conn.execute("SELECT COUNT(*) FROM records WHERE " + _rec_live_sql()).fetchone()[0]
                           + conn.execute("SELECT COUNT(*) FROM memories WHERE " + _live_sql()).fetchone()[0])
        except Exception:  # noqa: BLE001
            n_total = 0
        n_total = max(1, n_total)
        idf = {t: math.log(1.0 + (n_total - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5)) for t in q_tokens}
        total_mass = sum(idf.values()) or 1.0
    strong, weak = [], []
    for r in rows:
        if (r.get("_semantic_sim") or 0) >= _SEM_MIN_SIM:
            strong.append(r)
            continue
        text = " ".join(str(r.get(k) or "") for k in ("title", "content", "project")).lower()
        matched = [t for t in q_tokens if t.lower() in text]
        if not matched:
            weak.append(r)
            continue
        if _cjk_run(kw, text) >= 4:
            strong.append(r)   # 真短语命中（大迭代优化→迭代优化）
            continue
        if idf and sum(idf[t] for t in matched) / total_mass < 0.5:
            weak.append(r)     # 蹭中一两个常见词：idf 质量不过半
        elif len(matched) >= min_match:
            strong.append(r)
        else:
            weak.append(r)
    if strong or not weak:
        return strong
    if _latin_garbage_tokens(root, q_tokens):
        return []   # 乱输/拼错查询：0 命中比 5 条噪音好（工具层有拆词重查提示）
    for r in weak:
        r["_weak"] = True   # 合法但稀缺：保留并标记，调用方/用户可自行取舍
    return weak


def _tokenize(text: str, max_tokens: int = 64) -> list:
    """检索分词（P4.2 BM25 通道）：英文/数字按 \\w+ 词（小写），中文任意段相邻 2-gram
    （单字段保留原字）。与 _retry_bigrams 的区别：它只产出 ≥4 字长串的重试词表（原语义不动），
    本函数是全量分词——中文整串拆成 bigram 才能治"整串 LIKE 全或无"（基线实锤盲区①）。
    max_tokens 只该约束 query 侧；doc 侧必须传大值——截断会让长记忆后半无法命中（复盘实锤）。"""
    out: list = []
    for w in re.split(r"\s+", (text or "").strip()):
        if not w:
            continue
        cjk_spans = _CJK_ALL_RE.findall(w)
        rest = _CJK_ALL_RE.sub(" ", w).lower()
        for seg in cjk_spans:
            if len(seg) == 1:
                out.append(seg)
            else:
                out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        out.extend(re.findall(r"\w+", rest))
        if len(out) >= max_tokens:
            break
    return out[:max_tokens]


def _bm25_score(q_set: list, docs: list) -> list:
    """BM25 打分纯函数（P4.3b 泛化，memories/records 两表共用）。
    docs: [(row, Counter(tokens), dl)] → [(score, row)] 按分降序、同分 id 升序。"""
    n = len(docs)
    avgdl = sum(dl for _, _, dl in docs) / max(1, n)
    df: Counter = Counter()
    for _, tf, _dl in docs:
        df.update(tf.keys())
    scored: list = []
    for r, tf, dl in docs:
        s = 0.0
        for w in q_set:
            f = tf.get(w, 0)
            if not f:
                continue
            idf = math.log(1.0 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * f * (_BM25_K1 + 1.0) / (f + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * dl / max(avgdl, 1e-9)))
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda t: (-t[0], t[1]["id"]))
    return scored


def _bm25_rank(root: str, query: str, kind: str = "", limit: int = 20) -> list:
    """内存 BM25 通道（07 计划 P4.2）：active 全量拉行 → _tokenize 内存统计 → BM25 打分。
    数据量数千行毫秒级；>1 万行再议持久化倒排（不为未来规模做工程）。
    只读不计唤起（count_hits 语义由调用方负责）。query 内部自行分词——
    空格拆词对中文整串无效，正是本通道要治的病。返回 [{**row, "_bm25": 分数}]。"""
    q_set = list(dict.fromkeys(_tokenize(query)))
    if not q_set:
        return []
    sql = "SELECT * FROM memories WHERE " + _live_sql() + ""
    args: list = []
    if kind in KINDS:
        sql += " AND kind=?"
        args.append(kind)
    with db_conn(root) as conn:
        rows = conn.execute(sql, args).fetchall()
    if not rows:
        return []
    docs = []
    for r in rows:
        ckey = (root, r["id"])
        ent = _MEM_TOK_CACHE.get(ckey)
        if ent is None:
            toks = _tokenize(" ".join([r["content"] or "", r["tags"] or "", r["project"] or ""]),
                             max_tokens=2000)  # doc 侧不截断：截了长记忆后半无法命中（复盘实锤）
            ent = (Counter(toks), len(toks))
            _MEM_TOK_CACHE[ckey] = ent
        docs.append((r, ent[0], ent[1]))
    return [dict(r, _bm25=round(s, 4)) for s, r in _bm25_score(q_set, docs)[:max(0, min(int(limit), 100))]]


_RECORDS_TOK_CACHE: dict = {}  # (root, record_id) → (Counter, dl)：记录内容不可变（只增不改），缓存进程内安全
# 1559 条全文分词 ~300ms/查询（搜索页同步调用，用户抱怨过慢）→ 缓存后首查之外 <10ms
# key 必须含 root——不同库的 id 空间独立（adv 多 tmp 库实测串库：id=1 在各库是不同内容）

_MEM_TOK_CACHE: dict = {}  # (root, memory_id) → (Counter, dl)：与 _RECORDS_TOK_CACHE 对称；
# 记忆可被 edit_memory 改写 content/tags → 其 text_changed 分支负责 pop 失效，避免 BM25 命中旧文本


def _records_bm25_rank(root: str, query: str, limit: int = 30) -> list:
    """records 表的 BM25 通道（P4.3b）：匹配面 title+content+project（与 _records_query 一致）。
    记录正文长（数百字）且无 updated 列——recency 不适用（β 全局关闭中）。返回带 _bm25 分。"""
    q_set = list(dict.fromkeys(_tokenize(query)))
    if not q_set:
        return []
    with db_conn(root) as conn:
        rows = conn.execute("SELECT * FROM records WHERE " + _rec_live_sql()).fetchall()
    if not rows:
        return []
    docs = []
    for r in rows:
        ckey = (root, r["id"])
        ent = _RECORDS_TOK_CACHE.get(ckey)
        if ent is None:
            toks = _tokenize(" ".join([r["title"] or "", r["content"] or "", r["project"] or ""]),
                             max_tokens=2000)
            ent = (Counter(toks), len(toks))
            _RECORDS_TOK_CACHE[ckey] = ent
        docs.append((r, ent[0], ent[1]))
    return [dict(r, _bm25=round(s, 4)) for s, r in _bm25_score(q_set, docs)[:max(0, min(int(limit), 100))]]


def _records_query(root: str, words: list, limit: int) -> list:
    like_cond = "(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR project LIKE ? ESCAPE '\\')"
    score_sql = " + ".join(
        "(CASE WHEN title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' "
        "OR project LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END)" for _ in words)
    args: list = []
    for w in words:
        like = f"%{_like_escape(w)}%"
        args += [like, like, like]
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT *, ({score_sql}) AS score FROM records WHERE " + _rec_live_sql() + " AND "
            f"({' OR '.join([like_cond] * len(words))}) "
            "ORDER BY score DESC, date DESC, id DESC LIMIT ?",
            args + args + [max(1, min(limit, 100))])]


def search_records(root: str, kw: str, limit: int = 30) -> list:
    """多词评分检索：query 按空白切词，命中词数越多越靠前（同分按日期）。
    单词行为兼容旧版；评分让"多关键词"查询真正缩小范围而非取并集噪声。
    0 命中且 query 含 ≥4 字连续中文串时按 2-gram 重试（见 _retry_bigrams）。
    P4.3b：_RRF_ENABLED 时走 LIKE+BM25 双路 RRF 融合（与 memories 检索同架构），
    结果带 _channels；融合即宽容召回（部分 bigram 重叠也返回），无匹配才走 bigram 重试。"""
    words = [w for w in re.split(r"\s+", (kw or "").strip()) if w][:8]
    if not words:
        return []
    if _RRF_ENABLED:
        rows = _records_search_fused(root, kw, words, limit)
    else:
        rows = _records_query(root, words, limit)
    rows = _coverage_gate(root, rows, kw)
    if rows:
        return _mark_conflicts(_fold_near_dupes(rows))   # 去重 + 矛盾标记（records 与 memories 对称）
    retry = _retry_bigrams(kw)
    if not retry:
        return []
    rows = _records_query(root, retry, limit)
    rows = _coverage_gate(root, rows, kw)
    for r in rows:  # 放宽召回标记：真实大库里常见二字组合会命中弱相关，输出层须标注
        r["_bigram"] = True
    return _mark_conflicts(_fold_near_dupes(rows))


def _records_search_fused(root: str, kw: str, words: list, limit: int) -> list:
    """records 双路融合（P4.3b，与 memories 同架构）：LIKE 路 + BM25 路 RRF。
    差异：无 pinned/kind；tie-break 用 id（融合分并列罕见，date 语义由 LIKE 路单用时保留）。"""
    like_rows = _records_query(root, words, limit=100)
    bm25_rows = _records_bm25_rank(root, kw, limit=100)
    if not like_rows and not bm25_rows:
        return []
    trange = _parse_time_range(kw)                       # P0-2 时间语义：每查询解析一次
    scores: dict = {}
    channels: dict = {}
    for rank, r in enumerate(like_rows):
        scores[r["id"]] = scores.get(r["id"], 0.0) + 1.0 / (_RRF_K + rank + 1)
        channels.setdefault(r["id"], {})["like"] = rank + 1
    for rank, r in enumerate(bm25_rows):
        scores[r["id"]] = scores.get(r["id"], 0.0) + _RRF_W_BM25 / (_RRF_K + rank + 1)
        channels.setdefault(r["id"], {})["bm25"] = rank + 1
    sem_rows = _semantic_rank(root, kw, "records") if _RRF_W_SEMANTIC > 0 else []  # P0-3 语义第三路
    for eid, s, _ in sem_rows:
        scores[eid] = scores.get(eid, 0.0) + _RRF_W_SEMANTIC * s
        channels.setdefault(eid, {})["semantic"] = round(s, 4)
    by_id = {r["id"]: r for r in [*like_rows, *bm25_rows]}
    for eid, _, r in sem_rows:
        if eid in by_id:
            # 双通道行：词法版本可能缺 _semantic_sim，补上供覆盖度闸门判「语义背书」
            by_id[eid].setdefault("_semantic_sim", r.get("_semantic_sim"))
        else:
            by_id.setdefault(eid, r)   # 语义独有候选（词法未召回）也能进融合
    _apply_graph_channel(root, "record", scores, channels, by_id)  # 第四路：图遍历
    fused = []
    for mid, s in scores.items():
        r = dict(by_id[mid])
        r["_channels"] = channels[mid]
        b = _phrase_boost(r, kw)                          # P0-1 精确短语/标题加法 boost
        if b:
            r["_boost"] = b
            s += b
        tb = _time_boost(r, trange)                       # P0-2 时间语义加法 boost
        if tb:
            r["_time_boost"] = tb
            s += tb
        fused.append((s, mid, r))
    fused.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in fused[:max(0, min(int(limit), 100))]]


# ---------------------------------------------------------------- P3 工具契约（审计 / 索引维护）

def audit_add(root: str, agent: str, tool: str, project: str = "", summary: str = "",
              undo_kind: str = "", undo_ref: str = "") -> None:
    """写操作审计（P3-1）：MCP 层每次写成功后落一行，带 undo 句柄（kind+ref，可被撤销工具消费）。
    fail-open：审计失败不影响主流程。"""
    try:
        with db_conn(root) as conn:
            conn.execute(
                "INSERT INTO op_audit(ts, agent, tool, project, summary, undo_kind, undo_ref) "
                "VALUES(?,?,?,?,?,?,?)",
                (_now(), _norm_agent(agent) or "", tool, project, (summary or "")[:300],
                 undo_kind, undo_ref))
    except Exception:
        pass


def audit_list(root: str, limit: int = 20) -> list:
    """最近的写操作审计（倒序）。"""
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM op_audit ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 200)),))]


def flush_index(root: str, force: bool = False, batch: int = 64) -> dict:
    """语义索引维护（P3-1 hub_flush_index）：把缺失（force=True 时全部）向量补进 embeddings。
    文本口径与写入钩子 _ensure_emb_for、tools/build_embeddings.py 三处一致。
    fail-open：语义依赖缺失/模型装载失败 → 返回 {"error": ...}，不影响其余功能。"""
    out = {"records": 0, "memories": 0, "scanned": 0, "dim": None, "model": _EMB_MODEL_NAME}
    if not _HAS_ST:
        return {"error": "未安装 sentence-transformers（语义路未启用，其余功能不受影响）"}
    if not _ensure_emb():
        return {"error": "语义模型加载失败（stderr 有留痕；本地缓存缺失时需联网首装）"}
    _init_emb_table(root)
    with db_conn(root) as conn:
        have = {(r["entity"], r["eid"]) for r in conn.execute(
            "SELECT entity, eid FROM embeddings")}
        recs = [dict(r) for r in conn.execute(
            "SELECT id, title, content, project FROM records WHERE status='active'")]
        mems = [dict(r) for r in conn.execute(
            "SELECT id, content, tags, project FROM memories WHERE status='active'")]
    pending: list = []
    for r in recs:
        if force or ("records", r["id"]) not in have:
            blob = " ".join(x for x in (r.get("title") or "", r.get("content") or "",
                                        r.get("project") or "") if x).strip()
            if blob:
                pending.append(("records", r["id"], blob))
    for m in mems:
        if force or ("memories", m["id"]) not in have:
            blob = " ".join(x for x in (m.get("content") or "", m.get("tags") or "",
                                        m.get("project") or "") if x).strip()
            if blob:
                pending.append(("memories", m["id"], blob))
    out["scanned"] = len(pending)
    step = max(1, min(int(batch), 256))
    for i in range(0, len(pending), step):
        chunk = pending[i:i + step]
        vecs = _encode([t for _, _, t in chunk])
        if vecs is None:
            break
        for (ent, eid, _t), v in zip(chunk, vecs):
            _store_emb(root, ent, eid, v)
            out[ent] += 1
    out["dim"] = _EMB_DIM
    return out


# ---------------------------------------------------------------- P3-2 待办状态机 + 事件流

TASK_STATUSES = ("pending", "in_progress", "blocked", "done")
TASK_CN = {"pending": "待办", "in_progress": "进行中", "blocked": "阻塞", "done": "完成"}
CLAIM_TTL_HOURS = 2   # 认领超时自动释放（防死锁）：claimed_at 早于该时长的 in_progress 可被接管
DEFAULT_PRIORITY = 2  # M1（2026-10-12）：任务缺省优先级（0急 1高 2中 3低）
CRYSTAL_PREFIX = "结晶 #"  # M2：完成链结晶记录的标题前缀（幂等识别用）
# M10：前沿队列四段中文名（hub_next / GUI 工作台 / 心跳开工推送同源，防文案漂移）
TASK_GROUP_CN = {"ready": "可认领", "doing": "进行中", "takeover": "可接管", "waiting": "等待依赖"}

EVENT_KINDS = ("progress", "note", "error", "handoff")


def _claim_cutoff() -> str:
    """认领 TTL 截止时刻（早于它的 claimed_at 视为超时）。"""
    return (datetime.datetime.now() - datetime.timedelta(hours=CLAIM_TTL_HOURS)) \
        .isoformat(timespec="seconds")


def task_add(root: str, title: str, project: str = "", detail: str = "",
             blocked_by: str = "", agent: str = "", priority=2,
             deps: str = "", due: str = "") -> tuple:
    """新建任务（P3-2 状态机入口；M1 加工作图字段）。
    priority：0急~3低（缺省 2）；deps：前置任务 id 逗号分隔（未完成前不会被 hub_next
    推荐、不可认领）；due：截止日（可空）。返回 (错误或 "", 任务 id)。"""
    title = (title or "").strip()
    if not title:
        return "title 必填", 0
    try:
        pr = max(0, min(int(priority), 3))
    except (TypeError, ValueError):
        pr = DEFAULT_PRIORITY
    deps_norm = ",".join(str(x) for x in _parse_task_deps(deps))[:200]
    with db_conn(root) as conn:
        cur = conn.execute(
            "INSERT INTO tasks(project,title,detail,status,blocked_by,priority,deps,due,"
            "created,updated,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (project.strip(), title[:200], (detail or "").strip()[:2000],
             "blocked" if (blocked_by or "").strip() else "pending",
             (blocked_by or "").strip()[:200], pr, deps_norm, (due or "").strip()[:20],
             _now(), _now(), _norm_agent(agent) or ""))
        return "", cur.lastrowid


def task_list(root: str, status: str = "", owner: str = "", project: str = "",
              limit: int = 50) -> list:
    """任务列表（进行中 → 阻塞 → 待办 → 完成 排序，同级按 id 倒序）。"""
    q = "SELECT * FROM tasks WHERE 1=1"
    args: list = []
    if status:
        q += " AND status=?"
        args.append(status)
    if owner:
        q += " AND owner=?"
        args.append(_norm_agent(owner) or owner)
    if project:
        q += " AND project=?"
        args.append(project)
    q += (" ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'blocked' THEN 1 "
          "WHEN 'pending' THEN 2 ELSE 3 END, id DESC LIMIT ?")
    args.append(max(1, min(int(limit), 200)))
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(q, args)]


def task_claim(root: str, tid, agent: str) -> tuple:
    """原子认领（P3-2）：pending → in_progress(owner)。M1：前置（deps）未全部完成时拒绝认领。
    并发安全：单条 UPDATE…WHERE（前置条件全写进 SQL）+ rowcount 判胜负（SQLite 写事务天然串行，
    两家同时 claim 只可能一家 rowcount=1）。超时自动释放：in_progress 且 claimed_at 早于 TTL
    的可被他人接管（防死锁）。返回 (错误或 "", 任务行 dict)。"""
    who = _norm_agent(agent) or ""
    if not who:
        return "agent 必填（认领必须署名）", {}
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return f"任务 id 非法：{tid!r}", {}
    now = _now()
    cutoff = _claim_cutoff()
    with db_conn(root) as conn:
        pre = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if pre is None:
            return f"任务 #{tid} 不存在", {}
        pre = dict(pre)
        waiting = _task_deps_waiting(conn, pre)
        if waiting:
            names = "、".join(
                f"#{w['id']} {w['title'][:20]}（{TASK_CN.get(w['status'], w['status'])}）"
                for w in waiting[:4])
            more = " 等" if len(waiting) > 4 else ""
            return f"认领失败：任务 #{tid} 前置未完成——等待 {names}{more}", pre
        cur = conn.execute(
            "UPDATE tasks SET status='in_progress', owner=?, claimed_at=?, updated=? "
            "WHERE id=? AND status<>'done' AND ("
            "  status='pending' "
            "  OR (status='in_progress' AND owner=?) "
            "  OR (status='in_progress' AND claimed_at<>'' AND claimed_at < ?))",
            (who, now, now, tid, who, cutoff))
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return f"任务 #{tid} 不存在", {}
        row = dict(row)
        if cur.rowcount == 1:
            pass  # 认领成功：M4 开工资锚点移到 conn 事务外补写（见下方）
        elif row["status"] == "done":
            return f"认领失败：任务 #{tid} 已完成（{row['done_at'][:10]}）", row
        elif row["status"] == "in_progress" and row["owner"] != who:
            return (f"认领失败：任务 #{tid} 已被 {row['owner']} 认领"
                    f"（{row['claimed_at'][:16]}；超 {CLAIM_TTL_HOURS}h 未更新可自动接管）"), row
        elif row["status"] == "blocked":
            return (f"认领失败：任务 #{tid} 处于阻塞（等待 {row['blocked_by'] or '未知'}）；"
                    "解除后用 hub_task_update status=pending"), row
        else:
            return f"认领失败：任务 #{tid} 当前状态 {row['status']}", row
    # M4：认领成功自动存「开工资锚点」——断点续跑的第一块砖（崩溃/换人/隔天续跑时，
    # 检查点时间线的起点）。必须在 conn 事务**外**写：嵌套连接会被写锁挡（busy_timeout
    # 后 OperationalError，被 except 吞了就静默丢失——2026-10-12 实测踩坑）。
    # 失败只降级，不影响认领结果。
    try:
        checkpoint_save(root, tid, who,
                        f"开工：{row['title']}" + (f"——{row['detail'][:200]}"
                                                  if row["detail"] else ""),
                        kind="auto_claim")
    except Exception:  # noqa: BLE001
        pass
    return "", row


def task_update(root: str, tid, status: str = "", agent: str = "",
                blocked_by: str = "", detail: str = "", priority=None,
                deps: str = "", due: str = "") -> tuple:
    """任务状态流转（P3-2 + M1）：pending/in_progress/blocked/done。
    持有者约束：in_progress 任务只有 owner 本人（或空 agent=管理面）可流转；
    →pending 释放并清 owner；→done 记 done_at 并**自动结晶**（M2：独立事务，失败只降级为
    提示、不影响状态流转，结果附在返回行的 crystal 键）；done 后不可再改。
    M1 新字段：priority(0~3) / deps(前置 id，传 "-" 清除) / due(截止日，传 "-" 清除)。
    返回 (错误或 "", 行 dict)。"""
    who = _norm_agent(agent) or ""
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return f"任务 id 非法：{tid!r}", {}
    if status and status not in TASK_STATUSES:
        return f"status 只能是 {'/'.join(TASK_STATUSES)}", {}
    became_done = False
    with db_conn(root) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return f"任务 #{tid} 不存在", {}
        row = dict(row)
        if row["status"] == "done" and status and status != "done":
            return f"任务 #{tid} 已完成，不能改回 {status}", row
        if (row["status"] == "in_progress" and row["owner"] and who
                and row["owner"] != who):
            return f"任务 #{tid} 由 {row['owner']} 持有，只有持有者（或空传 agent）可流转", row
        became_done = bool(status == "done" and row["status"] != "done")
        sets: list = []
        args: list = []
        if status:
            sets.append("status=?")
            args.append(status)
            if status == "done":
                sets.append("done_at=?")
                args.append(_now())
            if status == "pending":
                sets.append("owner=''")
                sets.append("claimed_at=''")
            if status == "in_progress" and not row["owner"] and who:
                sets.append("owner=?")
                args.append(who)
                sets.append("claimed_at=?")
                args.append(_now())
        if (blocked_by or "").strip():
            sets.append("blocked_by=?")
            args.append(blocked_by.strip()[:200])
        if (detail or "").strip():
            sets.append("detail=?")
            args.append(detail.strip()[:2000])
        if priority is not None and str(priority).strip() != "":
            try:
                sets.append("priority=?")
                args.append(max(0, min(int(priority), 3)))
            except (TypeError, ValueError):
                pass
        ds = (deps or "").strip()
        if ds:
            if ds == "-":
                sets.append("deps=''")
            else:
                sets.append("deps=?")
                args.append(",".join(str(x) for x in _parse_task_deps(ds))[:200])
        du = (due or "").strip()
        if du:
            if du == "-":
                sets.append("due=''")
            else:
                sets.append("due=?")
                args.append(du[:20])
        if not sets:
            return ("没有要更新的字段（status/blocked_by/detail/priority/deps/due "
                    "至少传一个）", row)
        sets.append("updated=?")
        args.append(_now())
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*args, tid))
        out = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    if became_done:
        # M2 完成链自动结晶：独立事务，失败只降级为提示、不影响状态流转
        try:
            cerr, csum = task_crystallize(root, tid)
            out["crystal"] = csum if not cerr else f"未结晶（{cerr}）"
        except Exception as e:  # noqa: BLE001
            out["crystal"] = f"结晶异常（{type(e).__name__}: {str(e)[:80]}）"
    return "", out


def _parse_task_deps(deps) -> list:
    """'12, 15 #20' → [12, 15, 20]（全/半角逗号、空白、# 前缀都认；去重保序；非法项丢弃）。"""
    out: list = []
    for part in re.split(r"[,\s，、;；]+", str(deps or "")):
        part = part.strip().lstrip("#")
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            continue
        if n > 0 and n not in out:
            out.append(n)
    return out


def _task_deps_waiting(conn, row: dict) -> list:
    """任务的未完成前置清单 [{'id','title','status'}]；全完成 → []。
    前置 id 不存在也算等待（拼写错误要可见——静默放行比卡住更坏）。"""
    deps = _parse_task_deps(row.get("deps"))
    if not deps:
        return []
    marks = ",".join("?" * len(deps))
    rows = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT id,title,status FROM tasks WHERE id IN ({marks})", deps)}
    waiting = []
    for d in deps:
        r = rows.get(d)
        if r is None:
            waiting.append({"id": d, "title": "(不存在)", "status": "missing"})
        elif r["status"] != "done":
            waiting.append(r)
    return waiting


def task_next(root: str, project: str = "", owner: str = "", limit: int = 5) -> dict:
    """M1 前沿队列（Frontier）：依赖已满足、可直接开工的待办推荐；附进行中/可接管/等待。
    排序：priority（0 急 → 3 低）→ id（先来先做）。纯只读。返回结构化 dict 供 MCP 层排版。"""
    try:
        lim = max(1, min(int(limit or 5), 20))
    except (TypeError, ValueError):
        lim = 5
    q = "SELECT * FROM tasks WHERE status IN ('pending','in_progress','blocked')"
    args: list = []
    if project:
        q += " AND project=?"
        args.append(project)
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(q, args)]
        cutoff = _claim_cutoff()
        me = _norm_agent(owner) or ""
        ready, doing, takeover, waiting = [], [], [], []
        for r in rows:
            try:
                r["_priority"] = int(r.get("priority"))
            except (TypeError, ValueError):
                r["_priority"] = DEFAULT_PRIORITY
            if r["status"] == "in_progress":
                expired = bool(r["claimed_at"]) and r["claimed_at"] < cutoff
                if expired and r["owner"] != me:
                    takeover.append(r)
                elif (not me) or r["owner"] == me:
                    doing.append(r)
                continue
            wait = _task_deps_waiting(conn, r)
            if r["status"] == "blocked" or wait or (r["blocked_by"] or "").strip():
                r["_wait"] = wait
                waiting.append(r)
            else:
                ready.append(r)
    ready.sort(key=lambda t: (t["_priority"], t["id"]))
    doing.sort(key=lambda t: (t["_priority"], t["id"]))
    waiting.sort(key=lambda t: (t["_priority"], t["id"]))
    return {"ready": ready[:lim], "doing": doing[:lim], "takeover": takeover[:lim],
            "waiting": waiting[:lim],
            "counts": {"ready": len(ready), "doing": len(doing),
                       "takeover": len(takeover), "waiting": len(waiting)}}


def task_crystallize(root: str, tid, force: bool = False) -> tuple:
    """M2 完成链结晶：把已完成任务的完成过程（任务本身 + 完成期间的同项目记录）
    聚合为一条 record_type='crystal' 的工作记录——「做过什么、结果是什么」可回顾。
    原记录不删（可追溯）；幂等：同任务已有结晶则复用（force=true 软删旧结晶后重做）。
    返回 (错误或 "", 摘要文本)。"""
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return f"任务 id 非法：{tid!r}", ""
    olds: list = []
    with db_conn(root) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return f"任务 #{tid} 不存在", ""
        row = dict(row)
        if row["status"] != "done":
            return (f"任务 #{tid} 尚未完成（{TASK_CN.get(row['status'], row['status'])}），"
                    "完成后再结晶"), ""
        if not (row["project"] or "").strip():
            return f"任务 #{tid} 无项目归属，无法结晶（先给任务补 project）", ""
        olds = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM records WHERE record_type='crystal' AND status='active' "
            "AND title LIKE ?", (CRYSTAL_PREFIX + f"{tid}：%",))]
        if olds and not force:
            return "", f"已存在结晶（记录 #{olds[0]}；force=true 可重做）"
        rec_sql = ("SELECT id, agent, date, title FROM records WHERE project=? "
                   "AND status='active' AND record_type!='crystal' AND created>=?")
        rec_args: list = [row["project"], row["created"] or ""]
        if row.get("done_at"):
            rec_sql += " AND created<=?"
            rec_args.append(row["done_at"])
        rec_sql += " ORDER BY id ASC LIMIT 50"
        recs = [dict(r) for r in conn.execute(rec_sql, rec_args)]
    # 组装正文（事务外）：任务卡 + 关联记录清单
    owner = (row.get("owner") or "").strip()
    span = f"{(row['created'] or '')[:10]} → {(row.get('done_at') or row.get('updated') or '')[:10]}"
    lines = [f"> 自动结晶（M2）· 任务 #{tid}「{row['title'][:80]}」 · 完成 · "
             f"owner={owner or '未认领'} · {span}"]
    if (row.get("detail") or "").strip():
        lines.append(f"> 任务细节：{row['detail'].strip()[:200]}")
    if (row.get("deps") or "").strip():
        lines.append(f"> 前置链：{row['deps']}")
    lines.append("")
    if recs:
        lines.append(f"关联记录 {len(recs)} 条（同项目、任务创建后；原条目不删，可追溯）：")
        lines.append("")
        for r in recs:
            who = f"[{r['agent']}] " if r.get("agent") else ""
            lines.append(f"- #{r['id']} {who}{(r.get('date') or '')[:10]} "
                         f"{(r.get('title') or '')[:70]}")
    else:
        lines.append("关联记录 0 条（任务本身即最小结晶）。")
    rid = add_record(root, row["project"], owner, (row.get("done_at") or _now())[:10],
                     f"{CRYSTAL_PREFIX}{tid}：{row['title'][:60]}", "\n".join(lines),
                     source="hub", record_type="crystal")
    for oid in olds:  # force 重做：软删旧结晶（新结晶已落库；add 失败时旧结晶保留）
        with db_conn(root) as conn2:
            conn2.execute("UPDATE records SET status='deleted', valid_until=? WHERE id=?",
                          (_now()[:10], oid))
    return "", f"结晶完成：记录 #{rid}（关联 {len(recs)} 条）｜任务 #{tid}「{row['title'][:40]}」"


def event_add(root: str, content: str, kind: str = "progress", project: str = "",
              agent: str = "", ref: str = "") -> tuple:
    """写一条事件（P3-2 事件流：agent 写进展/订阅他人）。返回 (错误或 "", 事件 id)。"""
    content = (content or "").strip()
    if not content:
        return "content 必填", 0
    kind = kind if kind in EVENT_KINDS else "progress"
    with db_conn(root) as conn:
        cur = conn.execute(
            "INSERT INTO events(ts, agent, project, kind, content, ref) VALUES(?,?,?,?,?,?)",
            (_now(), _norm_agent(agent) or "", project.strip(), kind, content[:2000],
             (ref or "").strip()[:200]))
        return "", cur.lastrowid


def event_list(root: str, since_id: int = 0, limit: int = 30, project: str = "",
               agent: str = "") -> list:
    """事件流（倒序）；增量订阅传 since_id=上次最大 id 只取新事件。"""
    q = "SELECT * FROM events WHERE id>?"
    args: list = [max(0, int(since_id or 0))]
    if project:
        q += " AND project=?"
        args.append(project)
    if agent:
        q += " AND agent=?"
        args.append(_norm_agent(agent) or agent)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 30), 200)))
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(q, args)]


# ---------------------------------------------------------------- 数据完整性（0.1）

# 与 core.AGENT_PREFIX_RE 同源（多认 dsh）：识别历史平行目录 "deepseek - X" 的前缀
_AGENT_PREFIX_RE = re.compile(r"^(deepseek|hermes|claude|zcode|dsh|codex)\s*[-–—]\s*", re.IGNORECASE)


def strip_agent_prefix(name: str) -> str:
    """剥掉 "deepseek - X" 里的 agent 前缀，返回真实项目名。
    大小写不敏感；全半角空格与长短破折号都认（\\s 匹配全角空格）；
    多重前缀递归剥（"hermes - deepseek - X" → "X"）；非已知 agent 前缀原样返回。"""
    s = (name or "").strip()
    while True:
        m = _AGENT_PREFIX_RE.match(s)
        if not m:
            return s
        s = s[m.end():].strip()


def detect_duplicate_projects(root: str) -> list:
    """只读：找出被拆成多个 agent 前缀目录的同一项目，不改任何数据。
    首选名规则：无前缀者优先；都带前缀则取记录数最多者。按总量倒序。"""
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT project, COUNT(*) n FROM records WHERE status='active' GROUP BY project")]
    groups: dict = {}
    for r in rows:
        groups.setdefault(strip_agent_prefix(r["project"] or ""), []).append((r["project"], r["n"]))
    out = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        exact = [m for m in members if m[0] == key]
        canonical = exact[0][0] if exact else max(members, key=lambda x: x[1])[0]
        out.append({"canonical": canonical,
                    "members": sorted(m[0] for m in members),
                    "counts": {m[0]: m[1] for m in members},
                    "total": sum(m[1] for m in members)})
    out.sort(key=lambda g: -g["total"])
    return out


def detect_duplicate_records(root: str, min_len: int = 80) -> list:
    """只读：找出内容逐字相同的记录组。长度 ≤ min_len 的不算（避免空/极短内容误报）。"""
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, project, agent, date, content, substr(content,1,80) AS head FROM records "
            "WHERE status='active' AND length(content) > ? ORDER BY content, id", (min_len,))]
    out, cur, cur_key = [], [], None

    def flush():
        if len(cur) > 1:
            out.append({"ids": [x["id"] for x in cur],
                        "projects": sorted({x["project"] for x in cur}),
                        "agents": sorted({x["agent"] for x in cur}),
                        "head": cur[0]["head"]})

    for r in rows:
        if r["content"] != cur_key:
            flush()
            cur, cur_key = [], r["content"]
        cur.append(r)
    flush()
    out.sort(key=lambda g: -len(g["ids"]))
    return out


def migration_anchor(root: str) -> dict:
    """口径分离锚（0.1 第 2 步）：识别一次性迁移存量的时点并登记 meta（records_migrated_at），
    供体检按「迁移存量 vs AgentHub 时代新增」分口径展示——避免再得出 40:1 那种跨口径假象。
    依据 created 日期分布：单日占比 ≥50% 判为迁移日（真实库 94% 集中于 2026-09-30；
    迁移日当天 AgentHub 自己的新增记录也被计入存量，误差极小，属刻意接受的近似）。
    幂等：meta 已登记则沿用不改。"""
    with db_conn(root) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='records_migrated_at'").fetchone()
        total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        if row and row["value"]:
            at = row["value"]
        else:
            top = conn.execute(
                "SELECT substr(created,1,10) AS d, COUNT(*) AS n FROM records "
                "WHERE created!='' GROUP BY d ORDER BY n DESC LIMIT 1").fetchone()
            if not top or not total or top["n"] * 2 < total:
                return {"migrated_at": "", "migrated": 0, "era_new": total, "total": total}
            at = top["d"]
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('records_migrated_at',?)", (at,))
        migrated = conn.execute(
            "SELECT COUNT(*) FROM records WHERE substr(created,1,10)=?", (at,)).fetchone()[0]
    return {"migrated_at": at, "migrated": migrated, "era_new": total - migrated, "total": total}


def merge_duplicate_projects(root: str, canonical: str, aliases: list,
                             confirm: bool = False, agent: str = "") -> str:
    """把 aliases 项目的记录并入 canonical。**confirm=False 时只预演，绝不改数据**。

    安全约束（对抗用例要求）：
    - canonical 不得出现在 aliases 里；目标项目必须已存在；空参直接返回
    - 只 UPDATE records.project 与 memories.project，保留原 agent 字段（归属不丢）
    - 不删除任何记录；写 journal 落痕；文件系统目录不在本函数处理（走 hub_archive_project，可逆）"""
    aliases = [a for a in (aliases or []) if a]
    if not confirm:
        return (f"[预演] 将把 {len(aliases)} 个目录并入「{canonical}」：{'、'.join(aliases)}；"
                f"加 confirm=True 才执行")
    if not canonical or not aliases:
        return "参数为空，未执行"
    if canonical in aliases:
        return "canonical 不能出现在 aliases 里，未执行"
    with db_conn(root) as conn:
        if not conn.execute("SELECT 1 FROM records WHERE project=? LIMIT 1", (canonical,)).fetchone():
            return f"目标项目不存在：{canonical}，未执行"
        moved = mem_moved = 0
        for a in aliases:
            moved += conn.execute("UPDATE records SET project=? WHERE project=?",
                                  (canonical, a)).rowcount
            mem_moved += conn.execute("UPDATE memories SET project=? WHERE project=?",
                                      (canonical, a)).rowcount
    journal_add(root, agent or "user", "合并重复项目",
                f"{'、'.join(aliases)} → {canonical}（记录 {moved} 条 / 记忆 {mem_moved} 条）")
    return (f"已把 {len(aliases)} 个目录并入「{canonical}」：迁移记录 {moved} 条、记忆 {mem_moved} 条"
            f"（原 agent 字段已保留）")


# ---------------------------------------------------------------- 大脑记忆（核心）

def _clean_evidence(s: str) -> str:
    """规范化证据引用串：只保留 r:<记录id> / m:<记忆id>，去重保序，逗号连接。

    evidence 是「这条结论从前从哪来」——fleet-memory 治理四原语里的 provenance tracking
    的另一半（AgentHub 此前只有 writer，没有 derivation）。"""
    if s is None:
        return ""
    out, seen = [], set()
    for tok in re.split(r"[,\s;]+", str(s).strip()):
        tok = tok.strip().lower()
        if not tok:
            continue
        kind, _, num = tok.partition(":")
        if kind in ("r", "m") and num.isdigit():
            key = f"{kind}:{int(num)}"
            if key not in seen:
                seen.add(key)
                out.append(key)
    return ",".join(out)


def _live_sql(a: str = "") -> str:
    """「现行有效」记忆的 SQL 条件片段：未软删 + 未被取代 + 未过期。

    2026 业界共识（Graphiti 双时相 validFrom/validTo、Hindsight 按 mentioned_at 裁决冲突）：
    **失效 ≠ 删除**——旧版本保留可追溯，但不得再参与推送与检索。
    此前 AgentHub 只有连续的 salience 衰减，一条被更正的旧事实仍会以「高相似」推送，
    是最昂贵的错法：agent 照着过期路径干活。"""
    return (f"{a}status='active' AND {a}superseded_by=0 "
            f"AND ({a}valid_until='' OR {a}valid_until>=date('now'))")


def _rec_live_sql(a: str = "") -> str:
    """records 版「现行有效」条件（P1-1）：未软删 + 未失效。撤销 = status 软删 + valid_until 落值，
    行保留可追溯（双时相"失效≠删除"）；检索/召回路径一律走本过滤。"""
    return f"{a}status='active' AND ({a}valid_until='' OR {a}valid_until>=date('now'))"


def live_version(root: str, mid: int) -> int:
    """沿取代链追溯到最新的有效版本 id；本身就是有效则返回自身。链有环时停在环上。"""
    with db_conn(root) as conn:
        cur, guard = int(mid), 0
        seen = set()
        while cur and guard < 64 and cur not in seen:
            seen.add(cur)
            row = conn.execute(
                "SELECT superseded_by FROM memories WHERE id=?", (cur,)).fetchone()
            if not row or not row["superseded_by"]:
                return cur
            nxt = int(row["superseded_by"])
            if nxt <= 0:      # -1 = 无继任的作废
                return cur
            cur = nxt
            guard += 1
    return int(mid)


def supersede_memory(root: str, old_id: int, new_id: int) -> str:
    """显式「#new 取代 #old」：给已存在的两条记忆建立取代关系。返回错误或 ""。"""
    old_id, new_id = int(old_id or 0), int(new_id or 0)
    if old_id <= 0 or new_id <= 0:
        return "old_id / new_id 必须是正整数"
    if old_id == new_id:
        return "不能自己取代自己"
    with db_conn(root) as conn:
        a = conn.execute("SELECT id FROM memories WHERE id=?", (old_id,)).fetchone()
        b = conn.execute("SELECT id FROM memories WHERE id=?", (new_id,)).fetchone()
        if not a or not b:
            return f"记忆 #{old_id} 或 #{new_id} 不存在"
        conn.execute("UPDATE memories SET superseded_by=?, updated=? WHERE id=?",
                     (new_id, _now(), old_id))
        conn.execute("UPDATE memories SET superseded_by=0, updated=? WHERE id=?",
                     (_now(), new_id))
    return ""


def retire_memory(root: str, mid: int, reason: str = "") -> str:
    """废弃一条记忆且无继任（superseded_by=-1）：结论不再成立、也没有新版本可指。
    它会立刻退出推送与检索，但条目本体与 evidence 保留，GUI 里仍可查证。"""
    mid = int(mid or 0)
    if mid <= 0:
        return "id 必须是正整数"
    with db_conn(root) as conn:
        row = conn.execute("SELECT id FROM memories WHERE id=?", (mid,)).fetchone()
        if not row:
            return f"记忆 #{mid} 不存在"
        conn.execute(
            "UPDATE memories SET superseded_by=-1, updated=?, evidence=? WHERE id=?",
            (_now(), (_clean_evidence(reason) or
                      conn.execute("SELECT evidence FROM memories WHERE id=?",
                                   (mid,)).fetchone()["evidence"] or ""), mid))
    return ""


def memory_trace(root: str, mid: int) -> list:
    """证据链（双向）：返回该记忆的取代链与它引用的证据原文摘要。

    结构：[{"role":"chain"/"evidence","id":..,"kind":..,"content":..,"valid_until":..,
            "confidence":..,"superseded_by":..}]
    用于回答「这条结论从哪来、谁写的、现在还算不算数」——治理四原语中 provenance 的可读化。"""
    with db_conn(root) as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (int(mid or 0),)).fetchone()
        if not row:
            return []
        m = dict(row)
    out = [{"role": "self", "id": m["id"], "kind": m["kind"], "content": m["content"],
            "agent": m["agent"], "created": m["created"], "valid_until": m["valid_until"],
            "confidence": m["confidence"], "superseded_by": m["superseded_by"]}]
    # ① 向后：谁取代了它（含链式）
    cur = int(m["superseded_by"] or 0)
    guard = 0
    while cur > 0 and guard < 32:
        guard += 1
        with db_conn(root) as conn:
            r = conn.execute("SELECT * FROM memories WHERE id=?", (cur,)).fetchone()
        if not r:
            break
        out.append({"role": "superseded_by", "id": r["id"], "kind": r["kind"],
                    "content": r["content"], "agent": r["agent"], "created": r["created"],
                    "valid_until": r["valid_until"], "confidence": r["confidence"],
                    "superseded_by": r["superseded_by"]})
        cur = int(r["superseded_by"] or 0)
    if int(m["superseded_by"] or 0) == -1:
        out.append({"role": "retired", "id": 0, "content": "（已废弃，无继任版本）"})
    # ② 向前：它引用的证据
    for tok in [t for t in (m["evidence"] or "").split(",") if t]:
        kind, _, num = tok.partition(":")
        num = int(num)
        if kind == "r":
            with db_conn(root) as conn:
                r = conn.execute("SELECT id,project,title,content,agent,date FROM records "
                                 "WHERE id=?", (num,)).fetchone()
            if r:
                out.append({"role": "evidence", "ref": tok, "type": "record", "id": r["id"],
                            "content": (r["content"] or "")[:200], "project": r["project"],
                            "title": r["title"], "agent": r["agent"], "date": r["date"]})
        elif kind == "m":
            with db_conn(root) as conn:
                r = conn.execute("SELECT id,kind,content,agent,created FROM memories "
                                 "WHERE id=?", (num,)).fetchone()
            if r:
                out.append({"role": "evidence", "ref": tok, "type": "memory", "id": r["id"],
                            "content": (r["content"] or "")[:200], "kind": r["kind"],
                            "agent": r["agent"], "created": r["created"]})
    return out


def add_memory(root: str, content: str, kind: str = "note", tags: str = "",
               project: str = "", agent: str = "", pinned: bool = False,
               supersedes: int = 0, valid_until: str = "",
               confidence: float = 1.0, evidence: str = "",
               maturity: str = "draft") -> int:
    """写一条记忆。supersedes=N 表示本条取代 #N（旧的自动退出推送但仍可追溯）。
    valid_until=YYYY-MM-DD 表示有效期止（到期自动退出）。confidence 0~1。
    evidence 形如 "r:2038,m:76"（支持的记录/记忆 id）。
    maturity（M6）：draft 草稿（缺省，新记忆生来低信任）/ validated 已验证（写入者
    已确认过——如跨会话复核过的结论）/ core 核心（慎用，人工 pinned+高频才会自动到）。"""
    if kind not in KINDS:
        kind = "note"
    if maturity not in MATURITY_LEVELS:
        maturity = "draft"
    old = int(supersedes or 0)
    if old < 0:
        raise ValueError("supersedes 必须为正整数或 0")
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        conf = 1.0
    conf = max(0.0, min(1.0, conf))
    vu, ev = (valid_until or "").strip()[:10], _clean_evidence(evidence)
    _ensure_emb()  # 模型懒加载移出写事务（同 add_record）
    with db_conn(root) as conn:
        if old > 0 and not conn.execute("SELECT id FROM memories WHERE id=?",
                                        (old,)).fetchone():
            raise ValueError(f"被取代的记忆 #{old} 不存在")
        agent = _norm_agent(agent)
        _upsert_agent(conn, agent, project)
        tags = _register_tags(conn, _norm_tags(tags))  # P1-2：标签规约 + 注册（大小写归一）
        cur = conn.execute(
            "INSERT INTO memories(kind,content,tags,project,agent,pinned,created,updated,"
            "valid_until,confidence,evidence,maturity) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, content, tags, project, agent, 1 if pinned else 0,
             _now(), _now(), vu, conf, ev, maturity))
        nid = cur.lastrowid
        sync_links(conn, "memory", nid, content)  # P1-3：正文 [[双链]] → relations
        _ensure_emb_for(root, "memories", nid, content, tags, project, conn=conn)
        if old > 0:
            conn.execute("UPDATE memories SET superseded_by=?, updated=? WHERE id=?",
                         (nid, _now(), old))
        return nid


PUSH_LIMIT_PROJECT = 3   # 记忆推送：项目相关最多条数（项目教训优先于全局置顶）
PUSH_LIMIT_PINNED = 3    # 记忆推送：全局置顶最多条数（防置顶膨胀推高每次心跳成本）
PUSH_LIMIT_RELATED = 2   # 记忆推送：跨项目语义联想最多条数（联想阈值 0.12）
PUSH_CHAR_CAP = 600      # 记忆推送：输出总字数封顶
PUSH_THROTTLE_MIN = 10   # 同 agent+project 重复心跳的免推窗口（分钟）——推送内容还在会话上下文里


def recall_for(root: str, project: str, agent: str = "") -> list:
    """开工记忆推送（反射弧）三层：本项目相关 → 全局置顶 → 跨项目语义联想
    （该项目最近记录的 bigram 词集与其他记忆算 Jaccard，撞到别处踩过的坑自动想起）。
    命中即记一次唤起（use_count+1）——只对实际推送出去的条目计数（用进废退）。
    项目层与置顶层同款轮换排序（零唤起优先曝光）：纯 use_count 排序会让头部记忆
    越推越热、46 条记忆 39 条永远零唤起（2026-10-03 真实库实测的马太固化）。"""
    with db_conn(root) as conn:
        proj = [dict(r) for r in conn.execute(
            "SELECT * FROM memories WHERE " + _live_sql() + " AND project=? "
            "ORDER BY (use_count=0) DESC, use_count DESC, (maturity='draft') ASC, id DESC LIMIT ?",
            (project or "", PUSH_LIMIT_PROJECT))]
        pins = [dict(r) for r in conn.execute(
            "SELECT * FROM memories WHERE " + _live_sql() + " AND pinned=1 "
            "AND (project='' OR project IS NULL OR project!=?) "
            "ORDER BY (use_count=0) DESC, use_count DESC, (maturity='draft') ASC, id DESC LIMIT ?",
            (project or "", PUSH_LIMIT_PINNED))]
        # 跨项目语义联想：本项目最近 3 条记录的词集 vs 其他记忆。
        # 全局记忆（project 空）也入联想池——它们不置顶就三层全捞不到（2026-10-03 实测
        # 10 条全局 lesson/fact 零唤起的结构盲区），语义撞上就该想起
        related: list = []
        cur_txt = " ".join(r["content"] for r in conn.execute(
            "SELECT content FROM (SELECT content FROM records WHERE status='active' AND project=? "
            "ORDER BY id DESC LIMIT 3)", (project or "",)))
        cur_toks = _tokens(cur_txt)
        if len(cur_toks) >= 4:
            others = [dict(r) for r in conn.execute(
                "SELECT * FROM memories WHERE " + _live_sql() + " "
                "AND (project IS NULL OR project='' OR project!=?) LIMIT 300", (project or "",))]
            scored = []
            for om in others:
                ot = _tokens(om["content"])
                if not ot:
                    continue
                sim = len(cur_toks & ot) / len(cur_toks | ot)
                if sim >= 0.12:
                    scored.append((sim, om))
            scored.sort(key=lambda x: (x[0], x[1]["maturity"] != "draft",
                                       x[1]["use_count"] or 0), reverse=True)
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
            conn.execute("UPDATE memories SET use_count=use_count+1, last_hit=?, "
                         "maturity=CASE WHEN pinned=1 AND use_count+1>=? THEN 'core' "
                         "WHEN maturity='draft' AND use_count+1>=? THEN 'validated' "
                         "ELSE maturity END WHERE id=?",
                         (now, CORE_AT, VALIDATE_AT, r["id"]))
            r["use_count"] = (r["use_count"] or 0) + 1  # 返回值反映本次唤起后的计数
            r["last_hit"] = now
    # 推送落检索日志（tool=recall_push）：推送可观测，"推送 top10 是工作知识"才可验收
    log_search(root, "recall_push", (project or "")[:200], len(rows), agent=agent)
    # 推送内也要做矛盾标记（2026-10-13）：开工即见的记忆里若有互斥对，agent 会同时收到
    # 两个矛盾版本——和检索路径同一口径，别让 LLM 自己发现。
    return _mark_conflicts(rows)


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
    """检索日志（反馈回路）：谁/何时/用什么工具/查什么/命中几条。失败不影响检索本身。
    agent 过 _norm_agent——大小写/空白双身份会让检索统计分裂（实测 ZCode/zcode 各算各的）。"""
    try:
        with db_conn(root) as conn:
            conn.execute("INSERT INTO searches(ts,agent,tool,query,hits) VALUES(?,?,?,?,?)",
                         (_now(), _norm_agent(agent), tool, (query or "")[:200], max(0, int(hits or 0))))
    except sqlite3.Error:
        pass


def _memories_query(root: str, words: list, kind: str, limit: int) -> list:
    q = "SELECT * FROM memories WHERE " + _live_sql() + ""
    args: list = []
    if words:
        conds, score_sql = [], []
        for w in words:
            like = f"%{_like_escape(w)}%"
            conds.append("(content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' "
                         "OR project LIKE ? ESCAPE '\\')")
            args += [like, like, like]
            score_sql.append("(CASE WHEN content LIKE ? ESCAPE '\\' THEN 2 ELSE 0 END "
                             "+ CASE WHEN tags LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END)")
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


def _bump_access(root: str, ids: list, now: str = "") -> None:
    """命中即记一次唤起（与 recall_for 同款语义）：use_count+1 + last_hit。
    只对实际返回给调用方的条目计数——用进废退。空列表直接返回，不发 SQL。
    同时幂等埋一个口径锚：首次主动检索的时点（meta.active_recall_since），
    供体检报告说明 use_count 语义；仅作说明性记录，不参与任何判断。"""
    if not ids:
        return
    now = now or _now()
    with db_conn(root) as conn:
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('active_recall_since', ?)", (now,))
        for mid in ids:
            conn.execute("UPDATE memories SET use_count=use_count+1, last_hit=?, "
                         "maturity=CASE WHEN pinned=1 AND use_count+1>=? THEN 'core' "
                         "WHEN maturity='draft' AND use_count+1>=? THEN 'validated' "
                         "ELSE maturity END WHERE id=?",
                         (now, CORE_AT, VALIDATE_AT, mid))


def _parse_ts(ts) -> datetime.datetime | None:
    """宽松解析时间戳：空/None/非法一律返回 None（调用方按"从未发生"处理）。"""
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.datetime.fromisoformat(ts[:19])
    except ValueError:
        return None


def retention_score(mem: dict, now: datetime.datetime, actors: int = 1,
                    params: dict | None = None) -> float:
    """记忆保留分（纯函数，不碰数据库，便于测试）：越老越低、被访问越多越高。

    retention = salience·exp(−λ·age) + σ·ln(1+use)·exp(−μ·since_access)·breadth
    breadth 在 breadth_weight=0（默认）时为 1.0，即不影响结果。
    只用于展示与后续 curator 决策，从不参与检索排序（ai-memory 同款约束）。"""
    p = params or DECAY_PARAMS
    created = _parse_ts(mem.get("created"))
    age_days = max(0.0, (now - created).total_seconds() / 86400) if created else 365.0
    last = _parse_ts(mem.get("last_hit"))
    since = max(0.0, (now - last).total_seconds() / 86400) if last else age_days
    sal = float(mem.get("salience") or p["salience_default"])
    sal = max(p["salience_min"], min(p["salience_max"], sal))
    use = max(0, int(mem.get("use_count") or 0))
    breadth = 1.0 + p["breadth_weight"] * math.log(1 + max(int(actors or 1), 1) - 1)
    base = sal * math.exp(-p["lam"] * age_days)
    boost = p["sigma"] * math.log(1 + use) * math.exp(-p["mu"] * since)
    return max(0.0, base + boost * breadth)


def is_cold(mem: dict, now: datetime.datetime, params: dict | None = None) -> bool:
    """冷记忆判定：保留分低于阈值。第一阶段只用于展示，不触发任何自动动作。"""
    p = params or DECAY_PARAMS
    return retention_score(mem, now, params=p) < p["cold_threshold"]


SALIENCE_STEP = 0.25
_FEEDBACK_UP = {"helpful"}
_FEEDBACK_DOWN = {"not_helpful"}
_FEEDBACK_FLOOR = {"stale", "wrong"}
_FEEDBACK_ARCHIVE = {"archive"}


def set_memory_feedback(root: str, mid: int, feedback: str) -> str:
    """按反馈调节记忆：helpful 升档 / not_helpful 降档 / stale·wrong 直落地板（salience）；
    archive = 归档（status='archived'，不硬删——1.2 压缩沉淀后处理原记忆用）。
    非法枚举或坏 id 一律返回错误串，不改动任何数据（对抗用例要求）。"""
    fb = (feedback or "").strip().lower() if isinstance(feedback, str) else ""
    if fb not in _FEEDBACK_UP | _FEEDBACK_DOWN | _FEEDBACK_FLOOR | _FEEDBACK_ARCHIVE:
        return (f"反馈值非法：{feedback!r}（可选 helpful/not_helpful/stale/wrong/archive）")
    if not isinstance(mid, int) or mid <= 0:
        return f"记忆 id 非法：{mid!r}"
    p = DECAY_PARAMS
    with db_conn(root) as conn:
        row = conn.execute("SELECT id, salience FROM memories WHERE id=? AND " + _live_sql(),
                           (mid,)).fetchone()
        if not row:
            return f"记忆不存在或已归档：#{mid}"
        if fb in _FEEDBACK_ARCHIVE:
            conn.execute("UPDATE memories SET status='archived', updated=? WHERE id=?",
                         (_now(), mid))
            return f"记忆 #{mid} 已归档（status=archived，不硬删；恢复=置回 active）"
        cur = float(row["salience"] if row["salience"] is not None else p["salience_default"])
        if fb in _FEEDBACK_FLOOR:
            new = p["salience_min"]
        elif fb in _FEEDBACK_UP:
            new = min(p["salience_max"], cur + SALIENCE_STEP)
        else:
            new = max(p["salience_min"], cur - SALIENCE_STEP)
        conn.execute("UPDATE memories SET salience=?, updated=? WHERE id=?", (new, _now(), mid))
    return f"记忆 #{mid} 反馈「{fb}」已记录：salience {cur} → {new}"


_RRF_K = 60  # Hindsight 论文同款（07 计划 P4.3）
_RRF_W_BM25 = 2.0  # BM25 路权重：评测实锤等权融合稀释强相关（0.905→0.715），BM25 是主路
_RRF_W_RECENCY = 0.0  # recency 加成（P4.4）：评测三档定档 β=0.10→0.842 / 0.03→0.860 / 0→0.878，
# 当前规模（79 条记忆条条常被唤起）recency 与相关性负相关，默认关闭；机制保留（对抗用例验证），
# 触发重评条件=记忆规模显著增长或唤起分布变化（07 计划 v1.5）
_RRF_ENABLED = True  # 融合回滚开关：False 即回 LIKE 单路旧行为
_RRF_W_SEMANTIC = 1.5  # 语义路权重（P0-3 embedding 第三路；eval 标定；0=关闭独立降级到两路）
# 图路（第四路，Y3 接线 2026-10-13）：Graphiti/Zep 的检索是「向量 + BM25 + 图遍历」三路 RRF，
# 我们此前只有词法两路 + 语义一路，图谱造好了却没接进检索——与查询词**零重叠**的条目永远捞不回。
# 权重低于 BM25（主路），因为图路是补充召回不是主排序依据；0=关闭（可独立降级）。
_RRF_W_GRAPH = 1.0
_GRAPH_SEED_N = 8      # 从融合 top-N 取种子（业界做法：RRF 之后再扩展，不做全图遍历）
_GRAPH_HOPS = 2        # 跳数上限（2 跳以上扇出爆炸，业界共识）
_FOLD_THRESHOLD = 0.85  # 检索结果内近重复折叠阈值（bigram Jaccard，见 _fold_near_dupes）
# 检索结果矛盾检测阈值：相似到这个程度才有资格谈"互斥"（太低会把无关两条也判成矛盾）。
# 业界定位（theneuralbase「Conflicting memories」）：**冲突检测是检索后处理步骤——
# 取回之后、进上下文之前**，而不是等 LLM 自己发现（那样行为不确定且不可审计）。
_CONFLICT_SIM = 0.55  # 检索结果矛盾检测的**包含度**阈值（见 _mark_conflicts 为何不用 Jaccard）


def _mark_conflicts(rows: list, threshold: float = 0.0) -> list:
    """给同时命中的相似条目标 `_conflict_with`（疑似互斥，需人或 agent 显式裁决）。

    为什么用**包含度**而不是 Jaccard：「一条修正另一条」的典型形态是新条**包含**旧条全文
    再追加修正说明（"X 是 A——不对，实际是 B"）。Jaccard 会被长条稀释：短条 token 集是
    长条的子集时，Jaccard 只有 |短|/|长|，永远够不到阈值（实测 0.33）。
    包含度 |交|/min(|A|,|B|) 对这种"包含式修正"是 1.0，才对症。

    为什么只标不裁：仲裁策略（最新优先/权威源优先/置信度优先）各有利弊，
    替用户拍板的代价 > 多看一条标记。系统负责把"这两条可能互斥"这件事**说出来**。
    复用 consolidate 的 _verdict 判定（恰一条含修正词 → 疑似矛盾），不另造第二套口径。
    """
    thr = threshold or _CONFLICT_SIM
    if thr <= 0 or len(rows) < 2:
        return rows
    toks = [(i, _tokens(r.get("content") or r.get("title") or ""),
             r.get("kind") or "") for i, r in enumerate(rows)]
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            ta, tb = toks[i][1], toks[j][1]
            if not ta or not tb:
                continue
            inter = len(ta & tb)
            if not inter:
                continue
            containment = inter / min(len(ta), len(tb))
            if containment < thr:
                continue
            if _verdict(rows[i].get("content") or "", rows[j].get("content") or "",
                        toks[i][2] == toks[j][2]) != "疑似矛盾（一条修正另一条）":
                continue  # 只标互斥；"疑似重复"已由折叠处理，"相关"不是问题
            rows[i].setdefault("_conflict_with", []).append(rows[j]["id"])
            rows[j].setdefault("_conflict_with", []).append(rows[i]["id"])
    return rows


def _fold_near_dupes(rows: list, threshold: float = 0.0) -> list:
    """检索结果内近重复折叠：同一件事的多条只保留分最高的那条。

    为什么需要：库里必然有近似重复（M7 整合提案就是实证），一起返回时同一个事实在上下文里
    占好几个位置，既费 token 又把真相关的条目挤出 limit。业界混合检索流水线把它列为
    **固定的第 3 步**（RRF 之后、进上下文之前）：dedup before context。

    折叠=不返回，但在保留行上留 `_dup_n`（折了几条）——信息不丢，事后可查。
    阈值取高（0.85）：只折"几乎一模一样"的。宁可漏折，不可误折——
    误折会让 agent 少看到一条真不同的事实，这比多看一条重复贵得多。
    """
    thr = threshold or _FOLD_THRESHOLD
    if thr <= 0 or len(rows) < 2:
        return rows
    kept: list = []
    kept_toks: list = []
    for r in rows:
        t = _tokens(r.get("content") or r.get("title") or "")
        hit = -1
        for i, kt in enumerate(kept_toks):
            if not t or not kt:
                continue
            union = len(t | kt)
            if union and len(t & kt) / union >= thr:
                hit = i
                break
        if hit >= 0:
            kept[hit]["_dup_n"] = int(kept[hit].get("_dup_n") or 0) + 1
            continue
        kept.append(r)
        kept_toks.append(t)
    return kept


def _graph_expand(root: str, entity: str, seeds: list, limit: int = 30) -> list:
    """图扩展召回：从种子做 1~2 跳遍历，返回 [(eid, score)]（分数越高越「图上近」）。

    entity: "record" / "memory"（与 relations 表的 kind 口径一致）。
    计分：2.0/跳数 + 显式边（双链 mention / 手工 link）0.5 加成——
    显式边说明「有人认定它们有关」，比结构边（同项目/同标签）更能说明问题。
    """
    if not seeds or _RRF_W_GRAPH <= 0:
        return []
    try:
        with db_conn(root) as conn:
            adj = _graph_adj(conn)
    except sqlite3.Error:
        return []
    scores: dict = {}
    for seed in {(entity, str(s)) for s in seeds}:
        seen = {seed}
        frontier = [seed]
        for depth in range(1, _GRAPH_HOPS + 1):
            nxt = []
            for n in frontier:
                for nb, ek in adj.get(n, []):
                    if nb in seen:
                        continue
                    seen.add(nb)
                    nxt.append(nb)
                    if nb[0] != entity:
                        continue
                    try:
                        eid = int(nb[1])
                    except (TypeError, ValueError):
                        continue
                    scores[eid] = scores.get(eid, 0.0) + (2.0 / depth) + (
                        0.5 if ek in ("mention", "link") else 0)
            frontier = nxt
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:max(1, min(limit, 50))]


def _graph_expand_cross(root: str, src: str, dst: str, seeds: list, limit: int = 10) -> list:
    """跨类型图扩展：从 src 类型的种子出发，捞 dst 类型的邻居（分数 ×0.5）。

    为什么要单独一个函数而不是给 _graph_expand 加参数：同类型扩展是「一路召回」进 RRF，
    跨类型是「另一类的补充信息」，权重与去处都不同——混在一个函数里迟早搞乱。
    """
    if not seeds:
        return []
    try:
        with db_conn(root) as conn:
            adj = _graph_adj(conn)
    except sqlite3.Error:
        return []
    scores: dict = {}
    for seed in {(src, str(s)) for s in seeds}:
        seen = {seed}
        frontier = [seed]
        for depth in range(1, _GRAPH_HOPS + 1):
            nxt = []
            for n in frontier:
                for nb, ek in adj.get(n, []):
                    if nb in seen:
                        continue
                    seen.add(nb)
                    nxt.append(nb)
                    if nb[0] != dst:
                        continue
                    try:
                        eid = int(nb[1])
                    except (TypeError, ValueError):
                        continue
                    scores[eid] = scores.get(eid, 0.0) + ((2.0 / depth) + (
                        0.5 if ek in ("mention", "link") else 0)) * 0.5  # 跨类型减半
            frontier = nxt
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:max(1, min(limit, 20))]


def _cross_enrich(root: str, r: dict, limit: int = 30) -> None:
    """search_all 层跨类型图增强（原地改 r["records"]/r["memories"]）。

    来源（FalkorDB《Your Agent's Memory Problem Is a Retrieval Architecture Problem》）：
    "start in vectors for semantic discovery, end in the graph for structural enrichment"——
    从一类条目出发，沿图把**另一类**的结构关联带回来。
    跨类型是补充信息，权重减半、只追加不插队，绝不压制同类型结果。
    不放进 search_records/search_memories：那会破坏两者的返回契约（调用方预期同类型列表）。
    """
    if _RRF_W_GRAPH <= 0:
        return
    pairs = (("record", "memory"), ("memory", "record"))
    for src, dst in pairs:
        rows = r["records"] if src == "record" else r["memories"]
        if not rows:
            continue
        # 种子优先取**词法真正命中**的（带 like/bm25 通道），而不是融合后的前 N——
        # 否则图路/语义捞回的低相关条目会占满种子位，把真正该扩展的目标挤出去
        # （2026-10-13 实测：预置数据占满前 8，目标记录排到 8 名外，跨类型召回为空）。
        seeds = [x["id"] for x in rows
                 if "like" in (x.get("_channels") or {}) or "bm25" in (x.get("_channels") or {})]
        if not seeds:
            seeds = [x["id"] for x in rows]
        seeds = seeds[:_GRAPH_SEED_N]
        for eid, _sc in _graph_expand_cross(root, src, dst, seeds, limit=8):
            dst_rows = r["memories"] if dst == "memory" else r["records"]
            if any(x["id"] == eid for x in dst_rows):
                continue
            row = _rows_by_ids(root, dst, [eid]).get(eid)
            if not row:
                continue
            row["_cross"] = f"来自{src}"
            dst_rows.append(row)


def _rows_by_ids(root: str, entity: str, ids: list) -> dict:
    """按 id 批量取行（图路候选补齐用），只取现行有效的（失效条目不进检索）。"""
    if not ids:
        return {}
    ids = [int(i) for i in ids if str(i).lstrip("-").isdigit()][:50]
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    tbl, live = ("records", _rec_live_sql()) if entity == "record" else ("memories", _live_sql())
    try:
        with db_conn(root) as conn:
            rows = conn.execute(f"SELECT * FROM {tbl} WHERE id IN ({ph}) AND {live}", ids).fetchall()
    except sqlite3.Error:
        return {}
    return {int(r["id"]): dict(r) for r in rows}


def _apply_graph_channel(root: str, entity: str, scores: dict, channels: dict,
                         by_id: dict, limit: int = 30) -> None:
    """把图遍历作为第四路接进 RRF（原地改 scores/channels/by_id）。

    顺序按业界共识：**先融合前三路 → 取 top 种子 → 图扩展 → 二次计入 RRF**。
    不做全图遍历（扇出爆炸），也不把图当 boost（那样召不回零重叠条目，白做）。
    """
    if _RRF_W_GRAPH <= 0 or not scores:
        return
    seeds = [k for k, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:_GRAPH_SEED_N]]
    g_rows = _graph_expand(root, entity, seeds, limit=limit)
    if not g_rows:
        return
    missing = [eid for eid, _ in g_rows if eid not in by_id]
    if missing:  # 图路独有候选（词法/语义都没召回的）在这里补进候选池
        for eid, r in _rows_by_ids(root, entity, missing).items():
            by_id.setdefault(eid, r)
    for rank, (eid, _g) in enumerate(g_rows):
        if eid not in by_id:      # 已删/已失效的条目不进融合
            continue
        scores[eid] = scores.get(eid, 0.0) + _RRF_W_GRAPH / (_RRF_K + rank + 1)
        channels.setdefault(eid, {})["graph"] = rank + 1


def _recency_of(r: dict, now: datetime.datetime) -> float:
    """recency ∈ [0,1]：exp(-λ·天数)，时间锚=max(updated, last_hit)（P4.4）。
    λ 复用 DECAY_PARAMS（口径统一，不另立参数）。空白/畸形/未来时间戳 → 0（不加成不崩）。"""
    anchor = max(r.get("updated") or "", r.get("last_hit") or "")
    try:
        dt = datetime.datetime.fromisoformat(anchor)
    except (ValueError, TypeError):
        return 0.0
    days = (now - dt).total_seconds() / 86400
    if days < 0:
        return 0.0
    return math.exp(-DECAY_PARAMS["lam"] * days)


# P0-1 检索强化：连续子串命中 boost（fail-open；量级经 tools/eval_search.py 标定）
_PHRASE_BOOST = 0.04      # 整串查询连续出现（CJK 直接子串；拉丁词序敏感）→ 短词查询精准信号
_TOKEN_BOOST = 0.025      # 长查询词（≥3 字）在文档连续出现 → 弱相关信号
_TOKEN_BOOST_LONG = 0.02  # 长标识符（≥6 字，如 external-controller / bilibili）额外加成（BM25 拆 bigram 易低估）
_TITLE_BOOST = 0.02       # 任一查询词命中标题 → 中等信号
_BOOST_CAP = 0.12         # 单条 boost 上限，避免子串匹配盖过 BM25 相关性
_TIME_BOOST = 0.06         # P0-2 时间语义：查询含时间词且候选落在解析区间内 → 统一加法 boost（只重排、不引入新候选）

_CN_MONTH = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
             "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _month_from_token(t: str) -> int | None:
    t = (t or "").strip()
    if t.isdigit():
        v = int(t)
        return v if 1 <= v <= 12 else None
    return _CN_MONTH.get(t)


def _parse_time_range(q: str) -> tuple[str, str] | None:
    """P0-2 时间语义解析：把中文相对/绝对时间词转成 [lo, hi] 日期区间（YYYY-MM-DD）。
    无时间词 → None（调用方不触发 boost，fail-open，零回归风险）。
    支持：最近/近[N][天周月年]、今天、昨天、本周、上周、本月、上个月、X月初/X月。
    口径：以今天为锚；未来月份（如 13 月）回退去年；月初=当月 1~10 日，整月=全月。
    解析失败/畸形 → None（绝不抛异常、绝不返回错误区间污染排序）。"""
    q = (q or "").strip()
    if not q:
        return None
    today = datetime.date.today()
    td = datetime.timedelta
    # 最近/近（+ 可选 N + 单位），单位可省略（默认 30 天）
    m = re.search(r"(最近|近)\s*(\d{1,3})?\s*(天|日|周|星期|月|年)?", q)
    if m:
        n = int(m.group(2)) if m.group(2) else 30
        unit = m.group(3) or "天"
        days = {"天": n, "日": n, "周": n * 7, "星期": n * 7,
                "月": n * 30, "年": n * 365}[unit]
        lo = (today - td(days=days)).isoformat()
        return (lo, today.isoformat())
    if re.search(r"今天|今日", q):
        return (today.isoformat(), today.isoformat())
    if re.search(r"昨天|昨日", q):
        y = (today - td(days=1)).isoformat()
        return (y, y)
    if re.search(r"本(周|星期)|这(周|星期)", q):
        mon = today - td(days=today.weekday())
        return (mon.isoformat(), today.isoformat())
    if re.search(r"上(周|星期)", q):
        this_mon = today - td(days=today.weekday())
        last_sun = this_mon - td(days=1)
        last_mon = last_sun - td(days=6)
        return (last_mon.isoformat(), last_sun.isoformat())
    if re.search(r"本(月)|这个月", q):
        first = today.replace(day=1)
        return (first.isoformat(), today.isoformat())
    if re.search(r"上(个)?月", q):
        first_this = today.replace(day=1)
        last_end = first_this - td(days=1)
        last_start = last_end.replace(day=1)
        return (last_start.isoformat(), last_end.isoformat())
    # X月初 / X月（阿拉伯或中文数字）
    m = re.search(r"(十[一二]?|[一二三四五六七八九]|[0-9]{1,2})\s*月(初)?", q)
    if m:
        mon = _month_from_token(m.group(1))
        if mon:
            year = today.year
            if mon > today.month:
                year -= 1
            first = datetime.date(year, mon, 1)
            if m.group(2):                       # 月初：当月 1~10 日
                last = first + td(days=9)
            else:                                # 整月
                nxt = datetime.date(year + 1, 1, 1) if mon == 12 else datetime.date(year, mon + 1, 1)
                last = nxt - td(days=1)
            return (first.isoformat(), last.isoformat())
    return None


def _row_date(r: dict) -> str:
    """候选行的日期锚：records 用 date 列；memories 用 created（均 YYYY-MM-DD 前缀）。"""
    return (r.get("date") or r.get("created") or "")[:10]


def _time_boost(r: dict, trange: tuple[str, str] | None) -> float:
    """P0-2 时间语义加法 boost：仅当查询解析出时间区间、且候选日期落在区间内时加分。
    只重排、不引入新候选；无区间或日期为空/越界 → 0（fail-open）。"""
    if not trange:
        return 0.0
    d = _row_date(r)
    if not d:
        return 0.0
    lo, hi = trange
    if lo <= d <= hi:
        return round(_TIME_BOOST, 4)
    return 0.0


def _phrase_boost(r: dict, kw: str) -> float:
    """融合后加法 boost（两路融合共用）：
    ① 整串查询折叠空白后连续命中（CJK 直接子串；拉丁词序敏感）——短词查询的精准信号，治「ui 配色」
       这类连写即相关的查询；② 长查询词（≥3 字）逐词连续命中——专治 BM25 把长拉丁标识符拆 bigram
       后低估的命中（external-controller / bilibili），短高频词（ui/配色）不触发以免噪声。
    仅加分、不扣分；零命中面不改动排序。空白折叠兼容「UI配色」vs「ui 配色」空格差异。
    记忆表无 title 列，标题 boost 仅对 records 生效。"""
    q = (kw or "").strip().lower()
    if not q:
        return 0.0
    blob = " ".join([r.get("title") or "", r.get("content") or "",
                     r.get("tags") or "", r.get("project") or ""]).lower()
    blobc = re.sub(r"\s+", "", blob)
    words = [re.sub(r"\s+", "", w) for w in re.split(r"\s+", q) if w]
    boost = 0.0
    qc = re.sub(r"\s+", "", q)
    if qc and qc in blobc:                       # ① 整串短语
        boost += _PHRASE_BOOST
    for w in words:
        if len(w) < 3:                           # ② 仅长词，短高频词不触发
            continue
        if w in blobc:
            boost += _TOKEN_BOOST + (_TOKEN_BOOST_LONG if len(w) >= 6 else 0.0)
    title = (r.get("title") or "").lower()
    if any(w and w in title for w in words):
        boost += _TITLE_BOOST
    return round(min(boost, _BOOST_CAP), 4)


# ---------------------------------------------------------------- P0-3 语义检索（可选依赖，fail-open）
# 给 RRF 融合加第三路「语义向量」信号。依赖 sentence_transformers + 本地中文模型
# （BAAI/bge-small-zh-v1.5，数据不出本机）。零硬依赖：未安装 / 模型缺失 / 编码异常时全部降级——
# 融合只跑 LIKE+BM25 两路，MCP server 照常零依赖启动（铁律：外部能力用 fail-open 包裹）。
_EMB_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
_EMB = None          # 懒加载的 SentenceTransformer 实例（进程级缓存）
_EMB_DIM = None
_EMB_READY = None    # None=未尝试 / True / False
_EMB_LOCK = threading.Lock()  # 模型懒加载互斥：防并发写路径重复加载（8线程压测曾现）
_HAS_ST = False
# 语义候选准入门槛（2026-10-11 实测标定，见 _semantic_rank）：
# ① 查询须含中文——bge-small-zh 对 ASCII 标识符/路径/通配符查询的相似度不可信，且这类查询
#    多为字面查找（"50_off"/"100%"/"max_size"/"a.png"），放模糊召回会破坏 LIKE 精确契约
#    （实测 "50_off"→"50%_off" 余弦 0.642——纯阈值拦不住，必须按查询语种关断）。
# ② 余弦 ≥ 0.60——2026-10-17 R4 重新标定（旧值 0.45 已随嵌入数据漂移腐烂）：
#    17 查询实测，真查询 0.526~0.765 / 废话查询 0.559~0.640，分布重叠无分离阈值——
#    短查询对长文档的余弦在这个模型+语料上本就是弱信号。0.60 是「剪噪声带底」不是分离器：
#    留下的大多有语义背书价值（覆盖度闸门只信 ≥0.60 的），被剪掉的真查询有词汇路兜底
#    （截图 冒烟 0.526、部署 漂移 0.559 两词均在库中，词汇覆盖度照样强命中）。
#    语义路的定位因此收窄为：字面词不存在时的 paraphrase 桥（如「关掉错误」→关闭错误 0.638）。
_SEM_MIN_SIM = 0.60
_SEM_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
try:
    from sentence_transformers import SentenceTransformer  # 可选依赖（本机可无）
    _HAS_ST = True
except Exception:
    _HAS_ST = False


def _init_emb_table(root: str, conn=None) -> None:
    """语义向量表（fail-open 建表；不存在则语义路自动跳过）。
    传 conn 时复用调用方已开连接（写事务内建表，避免额外 WAL 写锁竞争）。"""
    ddl = """CREATE TABLE IF NOT EXISTS embeddings(
                entity TEXT NOT NULL, eid INTEGER NOT NULL, vec BLOB NOT NULL,
                model TEXT, dim INTEGER, updated TEXT, PRIMARY KEY(entity, eid))"""
    try:
        if conn is not None:
            conn.execute(ddl)
        else:
            with db_conn(root) as c:
                c.execute(ddl)
    except Exception as e:  # noqa: BLE001
        # fail-open 保留（语义路是可选能力，建不了表就跳过，绝不能拖垮主链路），
        # 但**绝不静默**——此前这里裸 `except: pass`，把「根目录不存在 / 库打不开」这类
        # 真因整个吞掉，症状表现为下游莫名其妙的 "no such table: embeddings"，
        # 与真因毫无关联，排查成本极高（R4 写语义回归时实锤踩到）。
        # 留痕到 stderr（不污染 stdout 协议流），与 MCP 层 [agenthub][tool-error] 同风格。
        try:
            sys.stderr.write(f"[agenthub][emb-table] 向量表不可用（{type(e).__name__}）：{e}"
                             f" —— 语义检索自动跳过，其余功能不受影响\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001  留痕本身失败也不许抛
            pass


def _ensure_emb() -> bool:
    """懒加载语义模型（进程级只试一次）。任何失败 → False，调用方降级到词法两路。
    双检锁：并发写路径首调用只加载一次，避免 8 线程压测重复加载 torch 拖垮。"""
    global _EMB, _EMB_DIM, _EMB_READY
    if _EMB_READY is not None:
        return _EMB_READY
    with _EMB_LOCK:
        if _EMB_READY is not None:
            return _EMB_READY
        if not _HAS_ST:
            _EMB_READY = False
            return False
        try:
            try:
                # 本地缓存优先（2026-10-11 线上实测教训）：网络不可达且代理关闭时，HF 联网检查
                # 会重试 ~5 分钟（实测 304s）最终失败 → fail-open 变成"语义路永久静默报废"。
                # 缓存已在 → local_files_only 纯本地秒载；缓存缺失 → 抛错后走联网下载（首次装配）。
                _EMB = SentenceTransformer(_EMB_MODEL_NAME, local_files_only=True)
            except Exception:
                _EMB = SentenceTransformer(_EMB_MODEL_NAME)
            try:
                _EMB_DIM = _EMB.get_embedding_dimension()
            except Exception:
                _EMB_DIM = _EMB.get_sentence_embedding_dimension()
            _EMB_READY = True
        except Exception as _exc:  # noqa: BLE001
            _EMB_READY = False
            try:  # 失败必须留痕：否则语义路静默降级，没人知道检索少了一路
                print(f"[agenthub] 语义模型加载失败，检索降级为词法两路：{_exc!r}",
                      file=sys.stderr, flush=True)
            except Exception:
                pass
        return _EMB_READY


_QUERY_VEC_CACHE: dict = {}   # R8：query 向量缓存（2026-10-17）——records/memories 两路各编一次同一 query 浪费一半语义耗时；agent 重复搜索同样命中。进程级，封顶 64 条防无界增长。


def _encode(texts: list) -> object | None:
    """批量编码（L2 归一化，余弦=点积）。无模型 / 异常 → None。

    R8：单条 query 走进程级缓存——语义路对 records/memories 各调用一次，同一 query
    编码两次纯浪费（实测单次编码 ~23ms，占 search_all 293ms 的 16%）。批量（>1）与
    写入路径不查缓存（向量要落库，缓存无意义）。
    """
    if not _ensure_emb():
        return None
    try:
        if len(texts) == 1 and texts[0] in _QUERY_VEC_CACHE:
            return _QUERY_VEC_CACHE[texts[0]]
        out = _EMB.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        if len(texts) == 1:
            if len(_QUERY_VEC_CACHE) >= 64:
                _QUERY_VEC_CACHE.clear()   # 简单封顶：清空重来（查询空间小，命中率损失可忽略）
            _QUERY_VEC_CACHE[texts[0]] = out
        return out
    except Exception:
        return None


def _store_emb(root: str, entity: str, eid: int, vec, conn=None) -> None:
    """存一条语义向量（fail-open；覆盖写）。vec 为 numpy float32 数组。
    传 conn 时复用调用方已开连接（写事务内建表+写入，避免额外 WAL 写锁竞争）。"""
    try:
        _init_emb_table(root, conn=conn)
        import datetime as _dt
        blob = vec.astype("float32").tobytes()
        sql = ("INSERT OR REPLACE INTO embeddings(entity, eid, vec, model, dim, updated) "
               "VALUES(?,?,?,?,?,?)")
        args = (entity, eid, blob, _EMB_MODEL_NAME, int(vec.shape[0]), _dt.datetime.now().isoformat())
        if conn is not None:
            conn.execute(sql, args)
        else:
            with db_conn(root) as c:
                c.execute(sql, args)
    except Exception:
        pass


def _ensure_emb_for(root: str, entity: str, eid: int, *texts, conn=None) -> None:
    """写入钩子：内容变更后同步语义索引。fail-open（编码失败忽略，该条降级无语义分）。
    conn 传入时复用外层已开连接（add_record/add_memory 写事务内写入，消除并发锁竞争）。"""
    if not _HAS_ST:
        return
    blob = " ".join((t or "") for t in texts).strip()
    if not blob:
        return
    vec = _encode([blob])
    if vec is not None:
        _store_emb(root, entity, eid, vec[0], conn=conn)


def _semantic_rank(root: str, query: str, entity: str, limit: int = 100) -> list:
    """语义第三路：编码 query → 与 embeddings 表余弦 → [(eid, rrf_score, row)]。
    仅 entity 在表中有向量时生效；否则空（fail-open）。语义独有的候选（词法未召回）也能进融合。
    准入门槛（2026-10-11 实测标定）：①查询含中文；②余弦 ≥ _SEM_MIN_SIM。见常量区注释。
    行形态与词法两路完全一致（SELECT * + 同一 live 过滤）：调用方（融合/唤起计数）按
    普通记忆行读 use_count/last_hit；被取代/过期/软删的行一概不得借语义路复活。"""
    if not query or not _SEM_CJK_RE.search(query):
        return []
    try:
        with db_conn(root) as c:
            if c.execute("SELECT COUNT(*) FROM embeddings WHERE entity=?", (entity,)).fetchone()[0] == 0:
                return []   # 空表短路：无索引时不加载模型（避免无谓开销）
    except Exception:
        return []
    if not _ensure_emb():
        return []
    qvec = _encode([query])
    if qvec is None:
        return []
    tbl = "records" if entity == "records" else "memories"
    live = _rec_live_sql("r.") if entity == "records" else _live_sql("r.")
    try:
        import numpy as np
        with db_conn(root) as c:
            rows = c.execute(
                f"SELECT e.eid, e.vec, r.* FROM embeddings e JOIN {tbl} r ON e.eid=r.id "
                f"WHERE e.entity=? AND {live}", (entity,)).fetchall()
        if not rows:
            return []
        q = qvec[0].astype("float32")
        qn = np.linalg.norm(q) + 1e-9
        # 批量矩阵点积：把所有 vec 堆成 (N,D) 矩阵，一次 (M@q) 替代逐行 np.dot + 逐行 norm
        # （原 O(N) Python 循环 → 单次 BLAS；search_all 对 records+memories 各跑一遍，放大为 2×O(N)）。
        vecs, kept = [], []
        for row in rows:
            v = np.frombuffer(row["vec"], dtype="float32")
            if v.shape[0] != q.shape[0]:
                continue
            vecs.append(v); kept.append(row)
        if not vecs:
            return []
        M = np.stack(vecs)                                    # (N, D)
        norms = np.linalg.norm(M, axis=1) + 1e-9
        sims = (M @ q) / (qn * norms)                        # 向量化余弦，与逐行公式数学等价
        scored = []
        for row, sim in zip(kept, sims):
            if sim < _SEM_MIN_SIM:   # 门槛：弱相关的语义候选不得进融合（噪声/误召回治理）
                continue
            r = dict(row)
            r.pop("vec", None)   # 剥掉 BLOB，保持行形态与词法路一致（几百字节/条）
            r.pop("eid", None)
            scored.append((row["id"], float(sim), r))
        scored.sort(key=lambda t: -t[1])
        out = []
        for rank, (eid, sim, r) in enumerate(scored[:limit]):
            r["_semantic_sim"] = round(sim, 4)
            out.append((eid, 1.0 / (_RRF_K + rank + 1), r))
        return out
    except Exception:
        return []


def _memories_search_fused(root: str, query: str, words: list, kind: str, limit: int) -> list:
    """P4.3 双路融合：LIKE/计分路（_memories_query）+ BM25 路（_bm25_rank）各自 rank →
    RRF(f)=Σ1/(k+rank) 融合（rank 天然免归一化，这是选 RRF 的理由）。
    pinned 不参与检索排序（评测三轮数据定案：硬前置 0.387 / 等权通道 0.449 / 纯相关 0.905
    ——pinned 弱相关条目会压过强相关结果；其曝光职责在心跳推送/hub_status/置顶徽章）。
    融合分乘 (1+β·recency)（P4.4）：同为命中时最近用到的排前；_channels 记录的是各路
    融合前 rank（boost 属融合后调整，分层可解释）。结果带 _channels={路: rank} 可解释（铁律 3）。
    两路全零命中返回 []——bigram 重试由调用方按原语义触发。"""
    like_rows = _memories_query(root, words, kind, limit=100)  # 候选池加深，融合后截 limit
    bm25_rows = _bm25_rank(root, query, kind, limit=100)
    if not like_rows and not bm25_rows:
        return []
    trange = _parse_time_range(query)                     # P0-2 时间语义：每查询解析一次
    scores: dict = {}
    channels: dict = {}
    for rank, r in enumerate(like_rows):
        scores[r["id"]] = scores.get(r["id"], 0.0) + 1.0 / (_RRF_K + rank + 1)
        channels.setdefault(r["id"], {})["like"] = rank + 1
    for rank, r in enumerate(bm25_rows):
        scores[r["id"]] = scores.get(r["id"], 0.0) + _RRF_W_BM25 / (_RRF_K + rank + 1)
        channels.setdefault(r["id"], {})["bm25"] = rank + 1
    sem_rows = _semantic_rank(root, query, "memories") if _RRF_W_SEMANTIC > 0 else []  # P0-3 语义第三路
    for eid, s, _ in sem_rows:
        scores[eid] = scores.get(eid, 0.0) + _RRF_W_SEMANTIC * s
        channels.setdefault(eid, {})["semantic"] = round(s, 4)
    by_id = {r["id"]: r for r in [*like_rows, *bm25_rows]}
    for eid, _, r in sem_rows:
        if eid in by_id:
            # 双通道行：词法版本可能缺 _semantic_sim，补上供覆盖度闸门判「语义背书」
            by_id[eid].setdefault("_semantic_sim", r.get("_semantic_sim"))
        else:
            by_id.setdefault(eid, r)   # 语义独有候选（词法未召回）也能进融合
    _apply_graph_channel(root, "memory", scores, channels, by_id)  # 第四路：图遍历
    fused = []
    now = datetime.datetime.now()
    for mid, s in scores.items():
        r = dict(by_id[mid])
        r["_channels"] = channels[mid]
        s *= 1.0 + _RRF_W_RECENCY * _recency_of(r, now)  # P4.4：融合分 recency 加成
        b = _phrase_boost(r, query)                       # P0-1 精确短语/标题加法 boost
        if b:
            r["_boost"] = b
            s += b
        tb = _time_boost(r, trange)                       # P0-2 时间语义加法 boost
        if tb:
            r["_time_boost"] = tb
            s += tb
        fused.append((s, mid, r))
    fused.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in fused[:max(0, min(int(limit), 100))]]


def search_memories(root: str, query: str = "", kind: str = "", limit: int = 20,
                    count_hits: bool = True) -> list:
    """检索记忆：多词评分（命中词数，与 records 检索一致）；无 query 时置顶优先按 id 倒序。
    0 命中且 query 含 ≥4 字连续中文串时按 2-gram 重试（与 search_records 同因）。

    count_hits：带 query 的实际检索命中才计唤起（use_count+1）——主动检索是最高价值的
    唤起信号，此前只统计被动推送，衰减/有用率的输入数据系统性失真（0.2 修复）。
    空 query 列清单不计；写入查重等内部调用须显式传 False。"""
    words = [w for w in re.split(r"\s+", (query or "").strip()) if w][:8]
    if _RRF_ENABLED:
        rows = _memories_search_fused(root, query or "", words, kind, limit)
    else:
        rows = _memories_query(root, words, kind, limit)  # 回滚开关：走旧单路
    rows = _coverage_gate(root, rows, query or "")
    if not rows and words:
        retry = _retry_bigrams(query)
        if retry:
            rows = _memories_query(root, retry, kind, limit)  # 放宽重试保持旧路径（融合外）
            rows = _coverage_gate(root, rows, query or "")
            for r in rows:  # 同 search_records：放宽召回须可识别
                r["_bigram"] = True
    if rows:
        rows = _fold_near_dupes(rows)   # 折叠在计唤起之前：被折掉的没进上下文，不该算唤起
        rows = _mark_conflicts(rows)    # 矛盾标记（业界：取回后、进上下文前检测，别让 LLM 自己发现）
    if rows and words and count_hits:
        now = _now()
        _bump_access(root, [r["id"] for r in rows], now)
        for r in rows:  # 返回值反映本次唤起后的计数（与 recall_for 一致）
            r["use_count"] = (r["use_count"] or 0) + 1
            r["last_hit"] = now
    return rows


def count_memories(root: str, kind: str = "") -> int:
    """活跃记忆总数（SQL COUNT；替代"全捞 1000 条到 Python 再 len"的低效计数）。"""
    q = "SELECT COUNT(*) FROM memories WHERE " + _live_sql() + ""
    args: list = []
    if kind in KINDS:
        q += " AND kind=?"
        args.append(kind)
    with db_conn(root) as conn:
        return conn.execute(q, args).fetchone()[0]


def memory_governance_stats(root: str) -> dict:
    """记忆治理健康度（批次 A·可靠性地基）：现行有效 vs 已被取代/过期/作废，以及证据链覆盖与平均置信度。
    只做统计、不改数据——让大脑的「取代链 + 双时相 + 证据链」治理效果可见、可验收
    （直接服务 2026-10-14 大脑验收标准的②③④）。"""
    with db_conn(root) as conn:
        rows = conn.execute(
            "SELECT id, status, superseded_by, valid_until, confidence, evidence "
            "FROM memories").fetchall()
    total = len(rows)
    live, superseded, retired, expired = 0, 0, 0, 0
    with_ev, conf_sum = 0, 0.0
    today = datetime.date.today().isoformat()
    for r in rows:
        sb = int(r["superseded_by"] or 0)
        if sb == -1:
            retired += 1
        elif sb > 0:
            superseded += 1
        else:
            # 现行：再看是否过期
            vu = (r["valid_until"] or "").strip()
            if vu and vu < today:
                expired += 1
            else:
                live += 1
        ev = (r["evidence"] or "").strip()
        if ev:
            with_ev += 1
        try:
            conf_sum += float(r["confidence"] or 1.0)
        except (TypeError, ValueError):
            conf_sum += 1.0
    return {
        "total": total,
        "live": live,
        "superseded": superseded,
        "retired": retired,
        "expired": expired,
        "with_evidence": with_ev,
        "evidence_coverage": round(with_ev / total, 3) if total else 0.0,
        "avg_confidence": round(conf_sum / total, 3) if total else 0.0,
    }


def metacognition(root: str) -> dict:
    """元认知（D10）：大脑的自我报告——「我记得什么 / 我忘了什么 / 我可能记错了什么」。
    只读聚合，不改数据。主动暴露自己的不靠谱处，是对外普遍缺失、我们成本极低的差异点。
    返回：coverage_blindspots（有记录零记忆的项目）/ low_confidence（低置信现行记忆）/
    dup_pairs（疑似矛盾对）/ pending_decisions（久未复核的决策记录）/ meta_ratio / governance。"""
    with db_conn(root) as conn:
        proj_rec = {r["project"]: r["n"] for r in conn.execute(
            "SELECT project, COUNT(*) n FROM records WHERE status='active' AND project!='' "
            "GROUP BY project")}
        proj_mem = {r["project"]: r["n"] for r in conn.execute(
            "SELECT project, COUNT(*) n FROM memories WHERE " + _live_sql()
            + " AND project!='' GROUP BY project")}
        lowconf = [dict(r) for r in conn.execute(
            "SELECT id, kind, content, confidence, project FROM memories WHERE " + _live_sql()
            + " AND confidence < 0.6 ORDER BY confidence ASC, id LIMIT 8")]
        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE " + _live_sql()).fetchone()[0]
        meta = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE " + _live_sql()
            + " AND (content LIKE '%AgentHub%' OR content LIKE '%hub\\_%' ESCAPE '\\' "
            "OR content LIKE '%大脑%')").fetchone()[0]
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
        dec_rows = [dict(r) for r in conn.execute(
            "SELECT id, project, date, title FROM records WHERE status='active' "
            "AND content LIKE '%决策记录%' AND date!='' AND date < ? "
            "ORDER BY date DESC LIMIT 8", (cutoff,))]
        # R6（2026-10-17）：元信息占比拆解——裸数字「61.8%」吓人但不可解读。按 kind 拆
        # + 零唤起对照，让「是不是灌水」一眼可判。实测结论：meta 唤起分布与非 meta
        # 持平（各 4 条零唤起），高唤起的全是规则/踩坑类真知识；占比高是因为本项目
        # 的工作产品就是大脑本身（project 类 78% meta 合理）——不追数，防误读。
        meta_sql = ("content LIKE '%AgentHub%' OR content LIKE '%hub\\_%' ESCAPE '\\' "
                    "OR content LIKE '%大脑%'")
        by_kind = {r[0]: {"meta": r[1], "total": r[2]} for r in conn.execute(
            f"SELECT kind, SUM(CASE WHEN {meta_sql} THEN 1 ELSE 0 END), COUNT(*) "
            f"FROM memories WHERE {_live_sql()} GROUP BY kind")}
        zero_meta = conn.execute(
            f"SELECT COUNT(*) FROM memories WHERE {_live_sql()} "
            f"AND use_count=0 AND ({meta_sql})").fetchone()[0]
        zero_nonmeta = conn.execute(
            f"SELECT COUNT(*) FROM memories WHERE {_live_sql()} "
            f"AND use_count=0 AND NOT ({meta_sql})").fetchone()[0]
    blindspots = sorted(
        [{"project": p, "records": n, "memories": proj_mem.get(p, 0)}
         for p, n in proj_rec.items() if not proj_mem.get(p)],
        key=lambda x: -x["records"])[:8]
    return {
        "coverage_blindspots": blindspots,
        "low_confidence": lowconf,
        "dup_pairs": len(similar_memories(root, limit=20)),
        "pending_decisions": [{"id": r["id"], "project": r["project"], "date": r["date"],
                               "title": r["title"]} for r in dec_rows],
        "meta_ratio": round(meta * 100 / max(1, total), 1),
        "meta_breakdown": {"by_kind": by_kind, "zero_use_meta": zero_meta,
                           "zero_use_non_meta": zero_nonmeta},
        "live_total": total,
        "governance": memory_governance_stats(root),
    }


def edit_memory(root: str, mid: int, content: str = "", kind: str = "", tags: str = "",
                pinned: bool | None = None, agent: str = "") -> str:
    """按 id 更新记忆字段（只改传入的）。返回错误或 ""。
    P1-1：改动落 memory_revisions（before/after 快照，可 diff / 可回滚）——此前内容编辑完全无痕。"""
    sets, args = [], []
    tag_idx = -1  # tags 在 args 中的位置（规约+注册表归一需在事务内做，先占位）
    if content:
        sets.append("content=?")
        args.append(content)
    if kind in KINDS:
        sets.append("kind=?")
        args.append(kind)
    if tags is not None and tags != "":
        tag_idx = len(args)
        sets.append("tags=?")
        args.append(tags)  # 占位：事务内规约 + 注册后回填
    if pinned is not None:
        sets.append("pinned=?")
        args.append(1 if pinned else 0)
    if not sets:
        return "没有要更新的字段"
    text_changed = ("content=?" in sets) or ("tags=?" in sets)
    if text_changed:
        _ensure_emb()  # 模型懒加载移出写事务（同 add_record/add_memory）
    with db_conn(root) as conn:
        before = _snapshot(conn, mid)
        if not before:
            return f"记忆 #{mid} 不存在"
        if tag_idx >= 0:
            args[tag_idx] = _register_tags(conn, _norm_tags(args[tag_idx]))  # P1-2 标签收口
        conn.execute(f"UPDATE memories SET {', '.join(sets)}, updated=? WHERE id=?",
                     (*args, _now(), mid))
        _revision(conn, mid, "edit", before, _snapshot(conn, mid), agent)
        if text_changed:
            _MEM_TOK_CACHE.pop((root, mid), None)  # 文本改了，BM25 token 缓存必须失效（否则命中旧文本）
            # 文本变更 → 同步语义索引（否则语义路拿着旧文本做相似度，编辑等于没生效）
            row = conn.execute("SELECT content, tags, project FROM memories WHERE id=?",
                               (mid,)).fetchone()
            if row:
                if "content=?" in sets:
                    sync_links(conn, "memory", mid, row["content"])  # P1-3：双链重抽（幂等）
                _ensure_emb_for(root, "memories", mid, row["content"], row["tags"],
                                row["project"], conn=conn)
    return ""


def delete_memory(root: str, mid: int, agent: str = "") -> str:
    """软删记忆（status='deleted'）。P1-1：删除留痕（before 快照入 memory_revisions，可追溯/可复活）。"""
    with db_conn(root) as conn:
        before = _snapshot(conn, mid)
        if not before:
            return f"记忆 #{mid} 不存在"
        conn.execute("UPDATE memories SET status='deleted', updated=? WHERE id=?", (_now(), mid))
        _revision(conn, mid, "delete", before, _snapshot(conn, mid), agent)
    return ""


# ---------------------------------------------------------------- P1-1 记忆版本化（可溯源/可回滚）

_REV_FIELDS = ("content", "kind", "tags", "pinned", "status")  # 可回滚字段全量（status 让"回滚删除"复活）


def _snapshot(conn, mid: int) -> dict:
    """修订快照：可回滚字段的现值。行不存在 → {}。"""
    r = conn.execute("SELECT content,kind,tags,pinned,status FROM memories WHERE id=?",
                     (mid,)).fetchone()
    return dict(r) if r else {}


def _revision(conn, mid: int, action: str, before: dict, after: dict, agent: str = "") -> None:
    """写一条记忆修订记录（同一事务内写；异常随事务回滚暴露，不吞）。"""
    conn.execute(
        "INSERT INTO memory_revisions(mid, action, before, after, agent, created) "
        "VALUES(?,?,?,?,?,?)",
        (mid, action, json.dumps(before, ensure_ascii=False),
         json.dumps(after, ensure_ascii=False), agent or "", _now()))


def memory_revisions(root: str, mid: int, limit: int = 50) -> list:
    """某条记忆的修订历史（新→旧）：谁/何时/改了啥（before/after JSON）。"""
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM memory_revisions WHERE mid=? ORDER BY id DESC LIMIT ?",
            (mid, max(1, min(int(limit), 500))))]


def memory_rollback(root: str, mid: int, rev_id: int, agent: str = "") -> str:
    """把记忆回滚到某次修订之前的状态（回滚 before 快照里有的字段：content/kind/tags/pinned/status
    ——含 status 所以"回滚删除"能真正复活记忆）。回滚本身也写一条 revision（action=rollback），
    可再回滚——版本链完整。返回错误或 ""。"""
    with db_conn(root) as conn:
        rev = conn.execute("SELECT * FROM memory_revisions WHERE id=? AND mid=?",
                           (rev_id, mid)).fetchone()
        if rev is None:
            return f"修订 #{rev_id} 不存在（记忆 #{mid}）"
        cur = conn.execute("SELECT content,kind,tags,pinned,project FROM memories WHERE id=?",
                           (mid,)).fetchone()
        if cur is None:
            return f"记忆 #{mid} 不存在"
    try:
        snap = json.loads(rev["before"] or "{}")
    except Exception:
        return "修订快照损坏，无法回滚"
    if not isinstance(snap, dict):
        return "修订无可回滚字段"
    fields = [k for k in _REV_FIELDS if k in snap]
    if not fields:
        return "修订无可回滚字段"
    text_changed = ("content" in fields) or ("tags" in fields)
    if text_changed:
        _ensure_emb()  # 正文可能变化 → 重嵌入；模型懒加载移出写事务
    with db_conn(root) as conn:
        before = _snapshot(conn, mid)
        conn.execute(f"UPDATE memories SET {', '.join(f'{k}=?' for k in fields)}, updated=? WHERE id=?",
                     (*[snap[k] for k in fields], _now(), mid))
        after = _snapshot(conn, mid)
        _revision(conn, mid, "rollback", before, after, agent)
        if text_changed:
            _ensure_emb_for(root, "memories", mid, after.get("content", ""), after.get("tags", ""),
                            cur["project"] or "", conn=conn)
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
    """全脑检索：工作记录 + 记忆 + 随记 + 文件名。返回 {"records": [...], "memories": [...], "notes": [...], "files": [...]}"""
    if not kw.strip():
        return {"records": [], "memories": [], "notes": [], "files": []}
    out = {"records": search_records(root, kw, limit=limit),
           "memories": search_memories(root, kw, limit=limit),  # 必须关键字：search_memories 第三参是 kind，
           "notes": search_notes(root, kw, limit),             # 位置传 limit 会把 limit 塞进 kind（2026-10-13 实测）
           "files": search_files(root, kw, limit)}
    _cross_enrich(root, out, limit)   # 跨类型图增强：从一类条目沿图把另一类带回来
    return out


def skill_sync(root: str, rows: list) -> dict:
    """技能索引导入（技能原生化·阶段1）。rows=[{name,agent,path,desc}]，按 (agent,path) 主键 UPSERT。

    不删行：技能目录里消失的条目也不删索引——元数据删了无法从库里恢复，而陈旧索引最多是
    召回一条不存在的技能（agent 读文件时会自己发现）。宁可脏，不可丢。
    """
    ensure_schema(root)  # serve 模式不建表（adv_e2e_schema 固化的行为）→ 首次导入前补建，幂等
    stat = {"added": 0, "updated": 0, "unchanged": 0}
    with db_conn(root) as conn:
        for r in (rows or []):
            path = str(r.get("path") or "")
            agent = str(r.get("agent") or "")
            if not path or not agent:
                continue
            name = str(r.get("name") or "")
            desc = str(r.get("desc") or "")
            old = conn.execute("SELECT desc FROM skills WHERE agent=? AND path=?",
                               (agent, path)).fetchone()
            if old is None:
                conn.execute("INSERT INTO skills(name,agent,path,desc) VALUES(?,?,?,?)",
                             (name, agent, path, desc))
                stat["added"] += 1
            elif (old["desc"] or "") != desc:
                conn.execute("UPDATE skills SET desc=?,name=? WHERE agent=? AND path=?",
                             (desc, name, agent, path))
                stat["updated"] += 1
            else:
                stat["unchanged"] += 1
    return stat


def skill_match(root: str, query: str, topk: int = 3, agent: str = "") -> list:
    """技能按需召回（技能原生化·阶段1 核心）：BM25 过 name+desc+agent，只回 top-N。

    替代「全量技能描述常驻上下文」——ZCode 165 个技能约 16.5k token 常驻，改按需回 3 条约 450 token。
    rowid AS id 是必需的：_bm25_score 同分排序取 row["id"]，缺了这个键会 KeyError（实测踩点）。
    """
    q_set = list(dict.fromkeys(_tokenize(query)))
    if not q_set:
        return []
    sql = "SELECT *, rowid AS id FROM skills"
    args: list = []
    if agent:
        # 大小写不敏感（2026-10-11 踩坑）：skills 表存显示名（WorkBuddy / Claude Code），
        # 而进程身份与调用方常传归一值（workbuddy / claude）——精确比对会静默返回 0 行，
        # 表面看就是「索引没导入」，极难排查。
        sql += " WHERE lower(agent)=lower(?)"
        args.append(agent)
    try:
        with db_conn(root) as conn:
            rows = conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        ensure_schema(root)  # 表还没建（索引从未导入过）：补建后重试一次，再失败则正常抛出
        with db_conn(root) as conn:
            rows = conn.execute(sql, args).fetchall()
    if not rows:
        return []
    docs = []
    for r in rows:
        # name ×2 加权：技能名信噪比最高（实测「留学选校」靠 name 拿到 13.2 分，desc 反而稀释）。
        # 不再拼 agent 名——它对同库所有行几乎相同，只会抬高平均文档长度、压低区分度（BM25 的 dl 惩罚）。
        # 单字母英文 token（如「B站」里的 "B"）是纯噪音——实测它把「抓B站字幕」的 top1
        # 送给了 weights-and-biases。中文单字要保留（分词后单个汉字是有效信号）。
        toks = [t for t in (_tokenize(r["name"] or "", max_tokens=200) * 2
                            + _tokenize(r["desc"] or "", max_tokens=4000))
                if len(t) >= 2 or _CJK_ALL_RE.search(t)]
        docs.append((r, Counter(toks), len(toks)))
    scored = _bm25_score(q_set, docs)
    # 去重键 = 父技能名（2026-10-11 子文件入索引后改为父级去重）：
    # ① ZCode 与 DSH 扫到同一批共享目录，同一技能会有多行；
    # ② 子文件条目 name 形如「父技能 ▸ 子文件名」——不去重的话同一父技能的多个子文件会挤爆 top-N。
    # 两种情况的键都是「▸」之前的父技能名；scored 已按分降序，天然保留同父里分最高的那条。
    out, seen = [], set()
    for s, r in scored:
        key = (r["name"] or "").split("▸")[0].strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(r, _bm25=round(s, 4)))
        if len(out) >= max(0, min(int(topk), 20)):
            break
    return out


def asset_sync(root: str, items: list) -> dict:
    """软件资产台账导入（技能原生化·阶段5，L3 数据层）。items=[{name,category,license,website,desc,...}]。

    与 skills 同策略：UPSERT 不删行。用途——agent 要装任何工具前先查大脑，
    不再每次去爬 accat.top，也不至于装到山寨源。
    """
    ensure_schema(root)
    stat = {"added": 0, "updated": 0, "unchanged": 0}
    with db_conn(root) as conn:
        for it in (items or []):
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            row = (name, str(it.get("category") or ""), str(it.get("license") or ""),
                   str(it.get("website") or ""), str(it.get("desc") or "")[:400],
                   str(it.get("source") or ""), str(it.get("version") or ""))
            old = conn.execute("SELECT desc FROM software WHERE name=?", (name,)).fetchone()
            if old is None:
                conn.execute("INSERT INTO software(name,category,license,website,desc,source,version)"
                             " VALUES(?,?,?,?,?,?,?)", row)
                stat["added"] += 1
            elif (old["desc"] or "") != row[4]:
                conn.execute("UPDATE software SET category=?,license=?,website=?,desc=?,source=?,"
                             "version=? WHERE name=?", row[1:] + (name,))
                stat["updated"] += 1
            else:
                stat["unchanged"] += 1
    return stat


def asset_find(root: str, kw: str, limit: int = 8) -> list:
    """软件资产检索（L3）：名字/分类/说明模糊匹配，装工具前先查这里。"""
    words = [w for w in re.split(r"\s+", (kw or "").strip()) if w][:6]
    if not words:
        return []
    conds = " OR ".join(["(name LIKE ? OR category LIKE ? OR desc LIKE ?)"] * len(words))
    args: list = []
    for w in words:
        pat = f"%{w}%"
        args.extend([pat, pat, pat])
    try:
        with db_conn(root) as conn:
            return [dict(r) for r in conn.execute(
                f"SELECT name,category,license,website,desc FROM software WHERE ({conds}) LIMIT ?",
                args + [max(0, min(int(limit), 30))]).fetchall()]
    except sqlite3.OperationalError:
        ensure_schema(root)  # 表未建（从未导入）→ 补建后返回空，不抛错
        return []


def skill_hit(root: str, agent: str, path: str) -> None:
    """命中回写：hits+1 / last_hit，供高频排序与冷技能清理。失败静默——统计不该阻断主流程。"""
    try:
        with db_conn(root) as conn:
            conn.execute("UPDATE skills SET hits=hits+1, last_hit=? WHERE agent=? AND path=?",
                         (datetime.datetime.now().isoformat(timespec="seconds"), agent, path))
    except Exception:  # noqa: BLE001
        pass


def search_files(root: str, kw: str, limit: int = 20) -> list:
    words = [w for w in re.split(r"\s+", (kw or "").strip()) if w][:8]
    if not words:
        return []
    conds = " OR ".join(["name LIKE ? ESCAPE '\\'"] * len(words))
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT project,name,path FROM files WHERE ({conds}) LIMIT ?",
            [f"%{_like_escape(w)}%" for w in words] + [max(1, min(limit, 100))])]


# ---------------------------------------------------------------- 人的随记（08 计划方向 A）

_NOTE_SOURCES = ("human", "agent", "web")
_NOTE_MOODS = ("", "兴奋", "笃定", "疑问", "纠结", "灵感", "低落", "平静")


# ---------------------------------------------------------------- 问大脑（D2：Dialectic API 零 LLM 版）

_ASK_INTENTS = (
    ("risk", ("风险", "踩坑", "坑", "教训", "容易错", "雷区", "别踩", "失败", "注意")),
    ("decision", ("决策", "为什么", "为何", "选型", "拍板", "取舍", "定过", "放弃", "方案")),
    ("todo", ("待办", "要做", "接下来", "下一步", "还没", "欠", "剩下", "未完成", "遗留")),
    ("status", ("状态", "进展", "进度", "现在怎样", "怎么样", "到哪", "哪些项目", "在做什么")),
    ("fact", ("是什么", "哪里", "哪个", "什么时候", "在哪", "怎么配", "端口", "路径", "版本", "多少", "是谁")),
)
_INTENT_CN = {"risk": "风险/教训", "decision": "决策依据", "todo": "欠账/待办",
              "status": "进展状态", "fact": "事实查询", "unknown": "未知"}
_DECISION_MARK = ("决策记录", "## 决策", "【决策", "选型", "取舍")


def _ask_intent(question: str) -> str:
    for name, kws in _ASK_INTENTS:
        if any(k in question for k in kws):
            return name
    return "fact"


def _guess_project(root: str, question: str) -> str:
    """问句里出现已知项目名则锁定（取最长匹配，避免子串误配）。"""
    hit = ""
    for p in list_projects(root, limit=300):
        name = p["name"] if isinstance(p, dict) else str(p)
        if name and name in question and len(name) > len(hit):
            hit = name
    return hit


def ask(root: str, question: str, project: str = "", agent: str = "") -> dict:
    """问大脑（D2）：把检索升级成「结构化回答 + 逐条出处 + 明确弃权」。
    零 LLM：规则判意图 → 复用现有检索取证据 → 模板成文；无依据时明说「库里没有」，绝不拼凑。
    只读：不写库、不计唤起（count_hits=False）。返回 dict（含 answer/citations/confidence/abstained）。"""
    q = (question or "").strip()
    if not q:
        return {"question": "", "intent": "unknown", "project": "", "citations": [],
                "confidence": "无", "abstained": True,
                "answer": "问题为空。请给出具体问题，例如「<项目名> 最大的风险是什么」。", "note": ""}
    intent = _ask_intent(q)
    proj = (project or "").strip() or _guess_project(root, q)
    # status 需要上下文：已知项目 / 明确问「项目·大脑·库·整体」；否则降级为 fact（可弃权，避免
    # 「X 的进展如何」被误判成报告全库状态）
    if intent == "status" and not proj and not any(
            k in q for k in ("项目", "大脑", "库", "所有", "哪些", "整体", "全局")):
        intent = "fact"
    cites: list = []
    parts: list = []

    def _pick_mem(rows, cap):
        if proj:
            rows = [m for m in rows if (m.get("project") or "") == proj] or rows
        out = []
        for m in rows:
            if len(out) >= cap:
                break
            out.append(m)
            cites.append({"type": "memory", "id": m["id"], "kind": m.get("kind", ""),
                          "project": m.get("project", "") or "",
                          "excerpt": (m.get("content") or "")[:140]})
        return out

    def _pick_rec(rows, cap):
        if proj:
            rows = [r for r in rows if (r.get("project") or "") == proj] or rows
        out = []
        for r in rows:
            if len(out) >= cap:
                break
            out.append(r)
            cites.append({"type": "record", "id": r["id"], "date": r.get("date", ""),
                          "project": r.get("project", "") or "",
                          "excerpt": (r.get("title") or r.get("content") or "")[:140]})
        return out

    scope = f"（限定项目：{proj}）" if proj else ""
    parts.append(f"关于「{q}」{scope}——问句意图判定：{_INTENT_CN[intent]}。")

    if intent == "risk":
        lessons = _pick_mem(search_memories(root, q, "lesson", 8, count_hits=False), 5)
        if not lessons:
            lessons = _pick_mem(search_memories(root, q, "", 8, count_hits=False), 4)
        if lessons:
            parts.append(f"已知的坑/教训（{len(lessons)} 条）：")
            for m in lessons:
                parts.append(f"· [{(m.get('kind') or '')}] {(m.get('content') or '')[:100]}（记忆#{m['id']}）")
        recs = _pick_rec(search_records(root, q, 8), 3)
        if recs:
            parts.append("相关记录（自行精读）：")
            for r in recs:
                parts.append(f"· {r.get('date','')}「{(r.get('title') or '')[:40]}」（记录#{r['id']}）")

    elif intent == "decision":
        recs = _pick_rec(search_records(root, q, 12), 4)
        decs = [r for r in recs if any(mk in (r.get("content") or "") for mk in _DECISION_MARK)]
        if decs:
            parts.append("相关决策记录（含「为什么」）：")
            for r in decs[:3]:
                for ln in (r.get("content") or "").splitlines():
                    if any(mk in ln for mk in _DECISION_MARK):
                        parts.append(f"· {ln.strip()[:100]}（记录#{r['id']}）")
        if recs:
            parts.append("背景记录：")
            for r in recs[:3]:
                parts.append(f"· {r.get('date','')}「{(r.get('title') or '')[:40]}」（记录#{r['id']}）")
        if not decs:
            parts.append("（未见结构化决策段；以上背景记录供参考。）")

    elif intent == "todo":
        todos = extract_todos(root, 60)
        if proj:
            todos = [t for t in todos if (t.get("project") or "") == proj] or todos
        if todos:
            parts.append(f"未办欠账（共 {len(todos)} 条，最多列 8）：")
            for t in todos[:8]:
                parts.append(f"· {t.get('todo','')[:100]}（记录#{t['id']} · {t.get('project','')}）")
                cites.append({"type": "record", "id": t["id"], "date": t.get("date", ""),
                              "project": t.get("project", "") or "",
                              "excerpt": (t.get("todo") or "")[:140]})
        else:
            parts.append("目前没有未办待办（未扫描到「后续/待验证/下一步」类线索）。")

    elif intent == "status":
        s = stats(root)
        parts.append(f"全库：记录 {s['records']} · 记忆 {s['memories']} · 项目 {s['projects']} · "
                     f"未关错误 {s.get('errors_open', 0)}。")
        if proj:
            gp = get_project(root, proj)
            if gp:
                parts.append(f"项目「{proj}」最后活跃 {gp.get('last_active','?')}，最近记录：")
                for r in gp.get("records", [])[:3]:
                    parts.append(f"· {r.get('date','')}「{(r.get('title') or '')[:40]}」")
                cites.append({"type": "project", "id": 0, "date": gp.get("last_active", ""),
                              "project": proj, "excerpt": f"项目 {proj} 活跃 {gp.get('last_active','?')}"})
        else:
            recent = get_progress(root, 5)
            if recent:
                parts.append("最近记录：")
            for r in recent:
                parts.append(f"· {r.get('date','')}「{(r.get('title') or '')[:40]}」（{r.get('project','')}）")
            if recent:
                cites.append({"type": "status", "id": 0, "excerpt": "全库状态快照（见上）"})

    else:  # fact
        mems = _pick_mem(search_memories(root, q, "", 8, count_hits=False), 5)
        if mems:
            parts.append("相关记忆：")
            for m in mems:
                parts.append(f"· [{(m.get('kind') or '')}] {(m.get('content') or '')[:100]}（记忆#{m['id']}）")
        recs = _pick_rec(search_records(root, q, 8), 3)
        if recs:
            parts.append("相关记录：")
            for r in recs:
                parts.append(f"· {r.get('date','')}「{(r.get('title') or '')[:40]}」（记录#{r['id']}）")

    # status/todo 的「没有」是确定答案（如「没有待办」），不算弃权；fact/risk/decision 的 0 证据才是弃权
    definitive = intent in ("status", "todo")
    abstained = len(cites) == 0 and not definitive
    if abstained:
        answer = (f"库里没有关于「{q}」的依据，我不知道。"
                  + (f"（限定项目：{proj}）" if proj else "")
                  + " 建议换个说法，或先用 hub_search / hub_get_progress 看有哪些相关线索。")
        confidence = "无"
    else:
        answer = "\n".join(parts)
        conf_seq = {0: "中"}  # 确定性的「没有」（todo/status 空态）
        confidence = conf_seq[0] if not cites else ("高" if len(cites) >= 3 else "中")
    return {"question": q, "intent": intent, "project": proj, "answer": answer,
            "citations": cites, "confidence": confidence, "abstained": abstained,
            "note": "零 LLM 模板回答：只引用库内证据，弱相关自行取舍。"}


def add_note(root: str, content: str, tags: str = "", link: str = "",
             mood: str = "", source: str = "human", open_at: str = "",
             context: str = "") -> int:
    """写入一条人的随记（08 计划 A-Ⅰ）。与 memories 分层：notes=第一人称原始思想，
    memories=结晶经验——检索互通（search_all），生命周期不同。mood 可选（零负担）。
    open_at 非空=时光胶囊（未到期对检索隐藏）；context=保存时的活跃项目/会话自动附。"""
    content = (content or "").strip()
    if not content:
        raise ValueError("content 必填")
    if source not in _NOTE_SOURCES:
        source = "human"
    mood = mood if mood in _NOTE_MOODS else ""
    with db_conn(root) as conn:
        cur = conn.execute(
            "INSERT INTO notes(ts,source,content,tags,link,mood,open_at,context) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (_now(), source, content[:4000], (tags or "").strip(), (link or "").strip(),
             mood, (open_at or "").strip(), (context or "").strip()))
        sync_links(conn, "note", cur.lastrowid, content)  # P1-3：随记里的 [[双链]] 也建关系
        return cur.lastrowid


def list_notes(root: str, limit: int = 20) -> list:
    """最近随记（ts 倒序）。GUI 随记卡片与每日回顾用。"""
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM notes ORDER BY id DESC LIMIT ?", (max(0, min(int(limit), 200)),))]


def notes_count(root: str, month: bool = False) -> int:
    """随记总数；month=True 只数本月（随记页统计行："你记了多少"的即时反馈）。"""
    with db_conn(root) as conn:
        if month:
            pre = datetime.datetime.now().strftime("%Y-%m")
            row = conn.execute("SELECT COUNT(*) FROM notes WHERE ts LIKE ?", (pre + "%",)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM notes").fetchone()
        return int(row[0]) if row else 0


def star_note(root: str, note_id: int, on: bool = True) -> None:
    """收藏/取消收藏（收藏条目进每日回顾池优先抽——journal-partner 模式）。"""
    with db_conn(root) as conn:
        conn.execute("UPDATE notes SET starred=? WHERE id=?", (1 if on else 0, note_id))


def get_note(root: str, note_id: int) -> dict | None:
    """按 id 取单条随记（hub_note_read 工具用：agent 深挖/整理的读取通道）。
    未到期胶囊对 agent 同样隐藏（与检索语义一致）。"""
    with db_conn(root) as conn:
        row = conn.execute(
            f"SELECT * FROM notes WHERE id=? AND {_note_visible_clause()}",
            (int(note_id), _now())).fetchone()
    return dict(row) if row else None


def latest_notes_for_agent(root: str, k: int = 3) -> list:
    """主人最新随记（排除未到期胶囊）——agent 开工视野：开工即看见主人最近在想什么。"""
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM notes WHERE {_note_visible_clause()} ORDER BY id DESC LIMIT ?",
            (_now(), max(1, min(int(k), 10))))]


def active_context(root: str) -> str:
    """最近活跃会话的项目名（30 分钟 TTL 内）——随记自动附上下文：
    "这条灵感是在改 XX 时冒出来的"。无活跃会话返回空串。"""
    try:
        sess = active_sessions(root)
    except Exception:
        return ""
    for s in sess:
        if s.get("project"):
            return s["project"]
        return ""
    return ""


def _note_visible_clause() -> str:
    """未到期时光胶囊对检索隐藏（GUI 时间线可见、灰显倒计时）。"""
    return "(open_at='' OR open_at<=?)"


def daily_recall_notes(root: str, k: int = 5) -> list:
    """每日随机回顾（flomo"不期而遇"模式）：随机抽旧随记，收藏优先。
    随机种子=当天日期——同一天内打开结果固定，跨天换一批。排除今天与未到期胶囊。"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM notes WHERE substr(ts,1,10)<>? AND {_note_visible_clause()} "
            "ORDER BY starred DESC, id DESC LIMIT 300", (today, _now()))]
    if not rows:
        return []
    import random
    rng = random.Random(today)  # 当日固定
    picked = rng.sample(rows, min(max(1, int(k)), len(rows)))
    # 抽样后恢复「收藏优先」：sample 会打乱顺序，不补排序则收藏优先形同虚设
    picked.sort(key=lambda r: (-(int(r.get("starred") or 0)), -(int(r.get("id") or 0))))
    return picked


def on_this_day_notes(root: str) -> list:
    """周年回放（On This Day）：往年今天的随记（按月-日匹配任意往年，最新年优先）。"""
    md = datetime.datetime.now().strftime("%m-%d")
    year = datetime.datetime.now().strftime("%Y")
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE ts LIKE ? AND substr(ts,1,4)<? ORDER BY ts DESC LIMIT 20",
            (f"____-{md}%", year))]


def capsule_notes(root: str, due_only: bool = False) -> list:
    """时光胶囊随记（open_at 非空）。due_only=True 只返回已到期（含今天）。"""
    with db_conn(root) as conn:
        if due_only:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM notes WHERE open_at!='' AND open_at<=? ORDER BY open_at DESC",
                (datetime.datetime.now().strftime("%Y-%m-%d"),))]
        return [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE open_at!='' ORDER BY open_at")]


def notes_heatmap(root: str, days: int = 365) -> list:
    """近 N 天 [(日期, 条数, 当日心情)]（心情取当日第一条非空）——热力图色带数据。"""
    start = (datetime.datetime.now() - datetime.timedelta(days=max(7, min(int(days), 730)) - 1)).strftime("%Y-%m-%d")
    with db_conn(root) as conn:
        counts = {r["d"]: r["n"] for r in conn.execute(
            "SELECT substr(ts,1,10) d, COUNT(*) n FROM notes "
            "WHERE substr(ts,1,10)>=? GROUP BY d", (start,))}
        moods = {}
        for r in conn.execute(
                "SELECT substr(ts,1,10) d, mood FROM notes "
                "WHERE substr(ts,1,10)>=? AND mood!='' ORDER BY id", (start,)):
            moods.setdefault(r["d"], r["mood"])
    out, cur = [], datetime.datetime.now() - datetime.timedelta(days=max(7, min(int(days), 730)) - 1)
    for i in range(max(7, min(int(days), 730))):
        d = (cur + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        out.append((d, counts.get(d, 0), moods.get(d, "")))
    return out


def search_notes(root: str, kw: str, limit: int = 20) -> list:
    """随记检索（LIKE 词面；纳入 search_all 实现人-agent 检索互通）。"""
    words = [w for w in re.split(r"\s+", (kw or "").strip()) if w][:8]
    if not words:
        return []
    conds = " OR ".join(
        ["(content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' OR mood LIKE ? ESCAPE '\\')"] * len(words))
    args: list = []
    for w in words:
        like = f"%{_like_escape(w)}%"
        args += [like, like, like]
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM notes WHERE ({conds}) AND {_note_visible_clause()} "
            "ORDER BY id DESC LIMIT ?",
            args + [_now(), max(0, min(int(limit), 100))])]


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
                "memories": one("SELECT COUNT(*) FROM memories WHERE " + _live_sql() + ""),
                "today": one("SELECT COUNT(*) FROM records WHERE status='active' AND date=?", (today,)),
                "errors_open": one("SELECT COUNT(*) FROM errors WHERE status='open'"),
                "searches_today": one("SELECT COUNT(*) FROM searches WHERE substr(ts,1,10)=?", (today,)),
                "searches_total": one("SELECT COUNT(*) FROM searches"),
                "projects_stalled": one("SELECT COUNT(*) FROM projects WHERE status='stalled'"),
                "agent_counts": agent_counts, "monthly_counts": monthly,
                "mem_governance": memory_governance_stats(root)}


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


# ---------------------------------------------------------------- 交接协议（2.2）

def create_handoff(root: str, project: str, agent: str, done: str = "",
                   next_step: str = "", excluded: str = "") -> str:
    """交接（handoff as a protocol）：换 agent 接手时写下 typed 固定字段——
    做完了什么 / 下一步 / 哪些路已排除。返回错误或 "已创建交接 #id"。"""
    project = (project or "").strip()
    agent = _norm_agent(agent)
    if not project:
        return "project 必填"
    if not agent:
        return "agent 必填"
    with db_conn(root) as conn:
        cur = conn.execute(
            "INSERT INTO handoffs(project,agent,done,next_step,excluded,created) VALUES(?,?,?,?,?,?)",
            (project, agent, (done or "")[:2000], (next_step or "")[:2000],
             (excluded or "")[:2000], _now()))
        hid = cur.lastrowid
    journal_add(root, agent, "创建交接", target=f"handoffs#{hid}",
                note=f"{project}（下一步：{(next_step or '')[:60]}）")
    return f"已创建交接 #{hid}（{project}，待接手 agent 认领）"


def list_handoffs(root: str, project: str = "", include_claimed: bool = False) -> list:
    """列交接：缺省只给 open（未认领），按 id 倒序（新在前）；include_claimed 时全量。"""
    q = "SELECT * FROM handoffs"
    args: list = []
    if not include_claimed:
        q += " WHERE status='open'"
        if project:
            q += " AND project=?"
            args.append(project)
    elif project:
        q += " WHERE project=?"
        args.append(project)
    q += " ORDER BY id DESC LIMIT 50"
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(q, args)]


def claim_handoff(root: str, handoff_id, agent: str) -> str:
    """认领（claimed exactly once）：单语句 UPDATE ... WHERE status='open'，
    SQLite 语句级原子性保证并发双认领恰一个 rowcount=1——不先查后改，无竞态窗口。
    状态锚 = 自增 id（铁律：不用时间戳）。返回错误或 "已认领交接 #id"。"""
    if not isinstance(handoff_id, int) or isinstance(handoff_id, bool) or handoff_id <= 0:
        return f"交接 id 非法：{handoff_id!r}"
    agent = _norm_agent(agent)
    if not agent:
        return "agent 必填"
    with db_conn(root) as conn:
        cur = conn.execute(
            "UPDATE handoffs SET status='claimed', claimed_by=?, claimed_at=? "
            "WHERE id=? AND status='open'", (agent, _now(), handoff_id))
        if cur.rowcount == 0:
            row = conn.execute("SELECT status, claimed_by FROM handoffs WHERE id=?",
                               (handoff_id,)).fetchone()
            if not row:
                return f"交接 #{handoff_id} 不存在"
            return f"交接 #{handoff_id} 已被 {row['claimed_by'] or '他人'} 认领（status={row['status']}），不可重复认领"
    journal_add(root, agent, "认领交接", target=f"handoffs#{handoff_id}")
    return f"已认领交接 #{handoff_id}（接手人：{agent}）"


# ---------------------------------------------------------------- 信号（M3）

SIGNAL_KINDS = ("msg", "ack_request", "alert")


def signal_send(root: str, from_agent: str, content: str, to_agent: str = "",
                kind: str = "msg", project: str = "") -> tuple:
    """M3 发信号：from → to（to 空=广播，全员可见）。返回 (提示或错误, 信号 id)。

    与 handoff 的分工：handoff 是「项目移交」（挂项目、typed 字段、待认领），
    signal 是「agent 间直接消息」（点对点/广播、已读回执、心跳提醒）。
    to 不在册不拒（新 agent 首秀可能还没心跳过），但返回里带拼写提醒。"""
    who = _norm_agent(from_agent)
    if not who:
        return "from_agent 必填", 0
    content = (content or "").strip()
    if not content:
        return "content 必填", 0
    kind = kind if kind in SIGNAL_KINDS else "msg"
    to = _norm_agent(to_agent) or ""
    if to == who:
        return "不能给自己发信号（收件箱里你自己的广播也能看到）", 0
    with db_conn(root) as conn:
        cur = conn.execute(
            "INSERT INTO signals(ts,from_agent,to_agent,project,kind,content) VALUES(?,?,?,?,?,?)",
            (_now(), who, to, (project or "").strip(), kind, content[:2000]))
        sid = cur.lastrowid
        known = bool(to) and bool(conn.execute(
            "SELECT 1 FROM agents WHERE name=?", (to,)).fetchone())
    note = f"{'→' + to if to else '→广播'}（{kind}）"
    if project.strip():
        note += f" [{project.strip()}]"
    journal_add(root, who, "发送信号", target=f"signals#{sid}", note=note)
    tip = f"已发送信号 #{sid}"
    if to and not known:
        tip += f"（提醒：{to} 未在册，可能拼错——hub_agents 可查在册名单）"
    return tip, sid


def signal_inbox(root: str, agent: str, unread_only: bool = False,
                 limit: int = 30) -> list:
    """收件箱：发给我的 + 广播（to 空），新的在前；每行附 _read（我是否已读）。
    unread_only=True 只要未读。自己发的广播也在（群里自己发的也显示）。"""
    who = _norm_agent(agent)
    if not who:
        return []
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM signals WHERE to_agent=? OR to_agent='' "
            "ORDER BY id DESC LIMIT ?", (who, max(1, min(int(limit or 30), 200))))]
        for r in rows:
            r["_read"] = bool(conn.execute(
                "SELECT 1 FROM signal_reads WHERE signal_id=? AND agent=?",
                (r["id"], who)).fetchone())
        if unread_only:
            rows = [r for r in rows if not r["_read"]]
    return rows


def signal_mark_read(root: str, signal_id, agent: str) -> str:
    """标记已读（幂等：signal_reads 按 (signal, agent) 主键 UPSERT，广播按人各记一次）。
    只能标自己的（点对点非我勿动；广播谁都能标）。返回提示或错误。"""
    who = _norm_agent(agent)
    if not who:
        return "agent 必填"
    try:
        sid = int(signal_id)
    except (TypeError, ValueError):
        return f"信号 id 非法：{signal_id!r}"
    with db_conn(root) as conn:
        row = conn.execute("SELECT id, to_agent FROM signals WHERE id=?", (sid,)).fetchone()
        if not row:
            return f"信号 #{sid} 不存在"
        if row["to_agent"] and row["to_agent"] != who:
            return f"信号 #{sid} 是发给 {row['to_agent']} 的，你不能标已读"
        cur = conn.execute(
            "INSERT OR IGNORE INTO signal_reads(signal_id,agent,read_at) VALUES(?,?,?)",
            (sid, who, _now()))
        if cur.rowcount == 0:
            return f"信号 #{sid} 你已读过（幂等，不重复记）"
        # 汇总态刷新（冗余字段；权威在 signal_reads）
        conn.execute("UPDATE signals SET status='read', read_at=?, read_by=? WHERE id=?",
                     (_now(), who, sid))
    journal_add(root, who, "已读信号", target=f"signals#{sid}")
    return f"信号 #{sid} 已标已读"


def signal_sent(root: str, agent: str, limit: int = 30) -> list:
    """我发出的信号（GUI 信号中心「已发送」tab 用）：from_agent=我 的点对点+广播，新在前。"""
    who = _norm_agent(agent)
    if not who:
        return []
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM signals WHERE from_agent=? ORDER BY id DESC LIMIT ?",
            (who, max(1, min(int(limit or 30), 200))))]


def signal_unread_count(root: str, agent: str) -> int:
    """未读数（心跳开工推送用）：发给我的 + 广播里我没读的。
    排除自己发的广播——提醒自己去读自己发的消息没有意义（收件箱仍显示，留痕用）。"""
    who = _norm_agent(agent)
    if not who:
        return 0
    with db_conn(root) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM signals s WHERE s.from_agent!=? "
            "AND (s.to_agent=? OR s.to_agent='') "
            "AND NOT EXISTS (SELECT 1 FROM signal_reads r "
            "WHERE r.signal_id=s.id AND r.agent=?)", (who, who, who)).fetchone()[0]


# ---------------------------------------------------------------- 检查点（M4）

CHECKPOINT_KINDS = ("manual", "auto_claim")


def checkpoint_save(root: str, task_id, agent: str, content: str,
                    kind: str = "manual") -> tuple:
    """M4 存检查点：任务级进展快照（断点续跑用）。返回 (错误或 "", 检查点 id)。

    kind=auto_claim 由 task_claim 自动存（开工资锚点）；manual 由 agent 在长任务
    执行中主动存。任务必须存在（防把检查点挂到幽灵任务上）。"""
    who = _norm_agent(agent)
    if not who:
        return "agent 必填", 0
    content = (content or "").strip()
    if not content:
        return "content 必填", 0
    kind = kind if kind in CHECKPOINT_KINDS else "manual"
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return f"任务 id 非法：{task_id!r}", 0
    with db_conn(root) as conn:
        row = conn.execute("SELECT id FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            return f"任务 #{tid} 不存在", 0
        cur = conn.execute(
            "INSERT INTO checkpoints(task_id,agent,ts,kind,content) VALUES(?,?,?,?,?)",
            (tid, who, _now(), kind, content[:2000]))
        return "", cur.lastrowid


def checkpoint_list(root: str, task_id, limit: int = 20) -> list:
    """某任务的检查点时间线（新在前）。"""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return []
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM checkpoints WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (tid, max(1, min(int(limit or 20), 100))))]


def checkpoint_latest(root: str, task_id):
    """某任务的最新一条检查点（没有则 None）——心跳 doing 行附它做进展摘要。"""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return None
    with db_conn(root) as conn:
        r = conn.execute(
            "SELECT * FROM checkpoints WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (tid,)).fetchone()
        return dict(r) if r else None


# ---------------------------------------------------------------- 哨兵（M5）

# 哨兵巡检项：全部**只读且无需 target 参数**（MCP server 进程里跑，不知道仓库路径）；
# 要 target 的（py_compile/git_status/deploy_diff/release/regression）由 agent 侧
# hub_ops_run 显式跑——哨兵只包「大脑自身健康」这类随时随地可判的。
SENTRY_CHECKS = ("rules_health", "brain", "hygiene", "dup_mem", "inbox", "stalled")


def sentry_sweep(root: str, cooldown_hours: int = 24) -> tuple:
    """M5 哨兵：跑关键体检项，把 gap 聚合成一条**广播 alert 信号**（所有 agent 开工即见，
    不用各自跑体检才知道大脑有情况）。返回 (摘要文本, 信号 id 或 0——0=无可报/去重跳过)。

    去重：与最近一条 system 广播 alert 内容相同且不足 cooldown_hours 不重复发（防刷屏）。
    检查项本身炸了（缺依赖等）跳过该项，不拖垮整轮。"""
    import ops  # noqa: PLC0415  延迟导入：ops 反向依赖 brain，模块级导入会成环
    gaps: list = []
    for name in SENTRY_CHECKS:
        try:
            r = ops.run_check(name, "", root)
        except Exception:  # noqa: BLE001  单项炸不拖垮整轮
            continue
        if r.get("ok"):
            continue
        gaps.append(f"· {name}：{(r.get('summary') or '')[:80]}")
    if not gaps:
        return "哨兵巡检：全部通过（无可报项）", 0
    # content 不带时间戳——时间戳信号表 ts 已有（收件箱会显示），带了反而让
    # 同 gap 集合的两次广播内容永远不同、冷却去重失效（2026-10-12 实测教训）。
    body = f"体检发现 {len(gaps)} 处需关注：\n" + "\n".join(gaps)
    # 去重：最近一条 system 广播 alert 同内容且未超 cooldown → 跳过
    with db_conn(root) as conn:
        recent = conn.execute(
            "SELECT ts, content FROM signals WHERE from_agent='system' "
            "AND to_agent='' AND kind='alert' ORDER BY id DESC LIMIT 1").fetchone()
    if recent and recent["content"] == body:
        try:
            age_h = (datetime.datetime.now()
                     - datetime.datetime.fromisoformat(recent["ts"])).total_seconds() / 3600
        except (TypeError, ValueError):
            age_h = 999
        if age_h < cooldown_hours:
            return (f"哨兵巡检：{len(gaps)} 处需关注，但 {age_h:.1f}h 前已广播过同样内容"
                    "（冷却中，不重复刷屏）"), 0
    tip, sid = signal_send(root, "system", body, to_agent="", kind="alert")
    if not sid:
        return f"哨兵巡检：{len(gaps)} 处需关注，但广播失败：{tip}", 0
    return f"哨兵巡检：{len(gaps)} 处需关注，已广播 #{sid}（所有 agent 下次开工可见）", sid


# ---------------------------------------------------------------- 隔离区（M8）

def _quarantine(root: str, table: str, rid, agent: str, reason: str = "") -> str:
    """M8 隔离（记录/记忆通用）：status active→quarantine。_live_sql/_rec_live_sql 只认
    active，隔离即退出检索与推送；但**不删**——可解除、可追溯（隔离区 ≠ 回收站）。
    与软删（deleted）的分工：deleted=基本不可见的撤销；quarantine=明确列出的观察区，
    批量处理后批量解除/转删。"""
    who = _norm_agent(agent)
    if not who:
        return "agent 必填"
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return f"id 非法：{rid!r}"
    cn = "记录" if table == "records" else "记忆"
    with db_conn(root) as conn:
        # 摘要列两表不同：records 有 title，memories 只有 content
        tcol = ("substr(title,1,40) AS t" if table == "records"
                else "substr(content,1,40) AS t")
        row = conn.execute(f"SELECT id, status, {tcol} FROM {table} WHERE id=?",
                           (rid,)).fetchone()
        if not row:
            return f"{cn} #{rid} 不存在"
        if row["status"] == "quarantine":
            return f"{cn} #{rid} 已在隔离区（无需重复）"
        if row["status"] != "active":
            return f"{cn} #{rid} 当前 status={row['status']}，只能隔离 active 的"
        conn.execute(f"UPDATE {table} SET status='quarantine' WHERE id=?", (rid,))
    journal_add(root, who, f"隔离{cn}", target=f"{table[:-1]}#{rid}", note=(reason or "")[:120])
    return (f"{cn} #{rid} 已隔离（{row['t'] or ''}）——退出检索与推送但未删除；"
            f"hub_quarantine action=release 可解除")


def _release(root: str, table: str, rid, agent: str) -> str:
    """M8 解除隔离：quarantine→active（回到检索/推送视野）。"""
    who = _norm_agent(agent)
    if not who:
        return "agent 必填"
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return f"id 非法：{rid!r}"
    cn = "记录" if table == "records" else "记忆"
    with db_conn(root) as conn:
        row = conn.execute(f"SELECT id, status FROM {table} WHERE id=?", (rid,)).fetchone()
        if not row:
            return f"{cn} #{rid} 不存在"
        if row["status"] != "quarantine":
            return f"{cn} #{rid} 不在隔离区（status={row['status']}）"
        conn.execute(f"UPDATE {table} SET status='active' WHERE id=?", (rid,))
    journal_add(root, who, f"解除隔离{cn}", target=f"{table[:-1]}#{rid}")
    return f"{cn} #{rid} 已解除隔离，回到检索与推送视野"


def quarantine_record(root: str, rid, agent: str, reason: str = "") -> str:
    return _quarantine(root, "records", rid, agent, reason)


def release_record(root: str, rid, agent: str) -> str:
    return _release(root, "records", rid, agent)


def quarantine_memory(root: str, mid, agent: str, reason: str = "") -> str:
    return _quarantine(root, "memories", mid, agent, reason)


def release_memory(root: str, mid, agent: str) -> str:
    return _release(root, "memories", mid, agent)


def quarantine_list(root: str, limit: int = 50) -> list:
    """隔离区清单（记录+记忆合并，新在前）：每行 src=record/memory + id + 摘要。
    注意用 src 而非 kind 做类型标记——memories 表自带 kind 列（fact/lesson…），
    同名键会被 dict 展开覆盖（2026-10-12 实测踩坑）。"""
    out: list = []
    with db_conn(root) as conn:
        for r in conn.execute(
                "SELECT id, substr(title,1,60) AS title, substr(content,1,80) AS content, "
                "project, agent, created FROM records WHERE status='quarantine' "
                "ORDER BY id DESC LIMIT ?", (max(1, min(int(limit or 50), 200)),)):
            out.append({"src": "record", **dict(r)})
        for r in conn.execute(
                "SELECT id, kind, substr(content,1,80) AS content, agent, created "
                "FROM memories WHERE status='quarantine' ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit or 50), 200)),)):
            out.append({"src": "memory", **dict(r)})
    out.sort(key=lambda x: x["id"], reverse=True)
    return out[:max(1, min(int(limit or 50), 200))]


# ---------------------------------------------------------------- 决策回路（M11）

REVIEW_VERDICTS = ("right", "wrong", "partial")


def decision_review_due(root: str, limit: int = 10) -> list:
    """到期未回顾的决策（M11）：review_date<=today 且 pending，附记录标题与决策段摘要。"""
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM decision_reviews WHERE status='pending' "
            "AND review_date!='' AND review_date<=date('now') "
            "ORDER BY review_date ASC, id ASC LIMIT ?",
            (max(1, min(int(limit or 10), 100)),))]
        for r in rows:
            rec = conn.execute("SELECT title, content, project FROM records WHERE id=?",
                               (r["record_id"],)).fetchone()
            r["title"] = (rec["title"] if rec else "") or f"记录 #{r['record_id']}"
            r["project"] = (rec["project"] if rec else "") or ""
            # 决策段摘要：抓「## 决策记录」后前两行，没有就正文前 60 字
            body = (rec["content"] if rec else "") or ""
            dec = ""
            for i, ln in enumerate(body.splitlines()):
                if _DECISION_HEAD_RE.match(ln.strip()):
                    dec = " ".join(l.strip() for l in body.splitlines()[i + 1:i + 3])[:100]
                    break
            r["decision_brief"] = dec or body[:60]
    return rows


def decision_review(root: str, review_id, verdict: str, note: str = "",
                    agent: str = "") -> str:
    """M11 记决策回顾结论：verdict=right/wrong/partial（对/错/部分对）。
    幂等：已 done 的再记会覆盖并留 journal（回顾可以改，历史可追溯）。"""
    who = _norm_agent(agent)
    if not who:
        return "agent 必填"
    try:
        rid = int(review_id)
    except (TypeError, ValueError):
        return f"回顾 id 非法：{review_id!r}"
    verdict = (verdict or "").strip()
    if verdict not in REVIEW_VERDICTS:
        return f"verdict 只能是 {'/'.join(REVIEW_VERDICTS)}"
    with db_conn(root) as conn:
        row = conn.execute("SELECT id, record_id, status FROM decision_reviews WHERE id=?",
                           (rid,)).fetchone()
        if not row:
            return f"回顾 #{rid} 不存在"
        conn.execute(
            "UPDATE decision_reviews SET status='done', verdict=?, note=?, agent=?, reviewed_at=? "
            "WHERE id=?", (verdict, (note or "")[:500], who, _now(), rid))
    journal_add(root, who, "决策回顾", target=f"records#{row['record_id']}",
                note=f"回顾 #{rid}：{verdict}" + (f"（{(note or '')[:60]}）" if note else ""))
    vcn = {"right": "当时决策正确", "wrong": "当时决策错误", "partial": "部分正确"}.get(verdict, verdict)
    return (f"决策回顾 #{rid} 已记：{vcn}"
            + (f"——{note[:80]}" if note else "")
            + "\n（结论挂在原决策记录上，hub_decision_review action=history 可查全部回顾史）")


def decision_review_history(root: str, limit: int = 20) -> list:
    """已回顾的决策史（新在前）——教训库：错过的决策比对的决策更有复盘价值。"""
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM decision_reviews WHERE status='done' "
            "ORDER BY reviewed_at DESC, id DESC LIMIT ?",
            (max(1, min(int(limit or 20), 100)),))]
        for r in rows:
            rec = conn.execute("SELECT title FROM records WHERE id=?", (r["record_id"],)).fetchone()
            r["title"] = (rec["title"] if rec else "") or f"记录 #{r['record_id']}"
    return rows


# ---------------------------------------------------------------- 整合（M7）

def consolidate_proposals(root: str, limit: int = 5) -> list:
    """M7 整合提案：把疑似重复/矛盾记忆组整理成可执行提案（只读）。
    整合是高危动作（可能丢信息）——系统只提案，执行要 consolidate_apply 显式确认。
    每组：{ids, kind, sim, verdict, contents}。"""
    sims = similar_memories(root, limit=limit * 2)
    props = []
    seen: set = set()
    for s in sims:
        key = (s["a"], s["b"])
        if key in seen:
            continue
        seen.add(key)
        props.append({"ids": [s["a"], s["b"]], "kind": "", "sim": s["sim"],
                      "verdict": s["verdict"],
                      "contents": [s["content_a"], s["content_b"]]})
        if len(props) >= limit:
            break
    return props


def consolidate_apply(root: str, ids, merged_content: str, agent: str) -> tuple:
    """M7 执行整合：写一条整合记忆（maturity=validated——整合即人工确认过的结论，
    evidence 挂原条目 id），原条目全部 superseded_by 指向新条（退出推送但可追溯）。
    返回 (错误或 "", 新记忆 id)。"""
    who = _norm_agent(agent)
    if not who:
        return "agent 必填", 0
    merged = (merged_content or "").strip()
    if not merged:
        return "merged_content 必填（整合后的完整内容，不是摘要）", 0
    try:
        id_list = sorted({int(x) for x in (ids or [])})
    except (TypeError, ValueError):
        return "ids 必须为整数列表", 0
    if len(id_list) < 2:
        return "至少 2 条才需要整合", 0
    with db_conn(root) as conn:
        ph = ",".join("?" * len(id_list))
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM memories WHERE id IN ({ph})", id_list)]
        if len(rows) != len(id_list):
            return f"有 id 不存在（传入 {len(id_list)}，库中命中 {len(rows)}）", 0
        live = [r for r in rows if r["superseded_by"] == 0]
        if len(live) < 2:
            return "现存条目不足 2 条（可能已被取代——先看提案）", 0
        kinds = {r["kind"] for r in live}
        kind = kinds.pop() if len(kinds) == 1 else "note"
        tags = ",".join(sorted({t.strip() for r in live
                                for t in (r["tags"] or "").split(",") if t.strip()}))[:200]
        project = live[0]["project"] or ""
    mid = add_memory(root, merged, kind, tags, project, who, maturity="validated",
                     evidence=",".join(f"m:{i}" for i in id_list))
    with db_conn(root) as conn:
        for i in id_list:
            conn.execute("UPDATE memories SET superseded_by=? WHERE id=?", (mid, i))
    journal_add(root, who, "整合记忆", target=f"brain:memories#{mid}",
                note="合并 " + ",".join(f"#{i}" for i in id_list))
    return "", mid


# ---------------------------------------------------------------- 视角（M9）

def view_follow(root: str, agent: str, scope_type: str = "project",
                scope_value: str = "") -> str:
    """M9 关注一个项目/标签（幂等 UPSERT）。"""
    who = _norm_agent(agent)
    if not who:
        return "agent 必填"
    st = (scope_type or "project").strip()
    sv = (scope_value or "").strip()
    if st not in ("project", "tag"):
        return "scope_type 只能是 project/tag"
    if not sv:
        return "scope_value 必填（项目名或标签）"
    with db_conn(root) as conn:
        conn.execute("INSERT OR IGNORE INTO agent_views(agent,scope_type,scope_value,created) "
                     "VALUES(?,?,?,?)", (who, st, sv, _now()))
    return f"已关注（{st}）：{sv}——该项目有进展会出现在你的心跳「[关注项目动态]」里"


def view_unfollow(root: str, agent: str, scope_type: str = "project",
                  scope_value: str = "") -> str:
    """M9 取消关注。"""
    who = _norm_agent(agent)
    if not who:
        return "agent 必填"
    st = (scope_type or "project").strip()
    sv = (scope_value or "").strip()
    with db_conn(root) as conn:
        cur = conn.execute("DELETE FROM agent_views WHERE agent=? AND scope_type=? "
                           "AND scope_value=?", (who, st, sv))
    return (f"已取消关注（{st}）：{sv}" if cur.rowcount
            else f"你本来就没关注（{st}）：{sv}")


def view_list(root: str, agent: str) -> list:
    """M9 我的关注清单。"""
    who = _norm_agent(agent)
    if not who:
        return []
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT scope_type, scope_value, created FROM agent_views WHERE agent=? "
            "ORDER BY created, scope_value", (who,))]


def view_recent(root: str, agent: str, exclude_project: str = "",
                limit: int = 3, days: int = 7) -> list:
    """M9 关注项目的最近进展（心跳附加段用）：关注的项目近 days 天有记录就返回，
    排除当前项目（避免与「今日任务」重复刷）。"""
    who = _norm_agent(agent)
    if not who:
        return []
    with db_conn(root) as conn:
        views = [r["scope_value"] for r in conn.execute(
            "SELECT scope_value FROM agent_views WHERE agent=? AND scope_type='project'",
            (who,))]
    if not views:
        return []
    views = [v for v in views if v != (exclude_project or "")]
    if not views:
        return []
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    out: list = []
    with db_conn(root) as conn:
        for v in views:
            r = conn.execute(
                "SELECT id, project, title, date, agent FROM records WHERE " + _rec_live_sql()
                + " AND project=? AND date>=? ORDER BY id DESC LIMIT 1", (v, cutoff)).fetchone()
            if r:
                out.append(dict(r))
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:max(1, min(int(limit or 3), 10))]


# ---------------------------------------------------------------- 技能资产化（M12）

def skill_stats(root: str) -> dict:
    """M12 技能资产化·统计：哪些技能真被用（hits）、哪些零使用（清理候选）。
    返回 {total, by_agent, top_used, zero_used}——零使用清单供人决定清理还是补说明。"""
    with db_conn(root) as conn:
        total = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
        by_agent = {r[0] or "未知": r[1] for r in conn.execute(
            "SELECT agent, COUNT(*) FROM skills GROUP BY agent ORDER BY 2 DESC")}
        top = [dict(r) for r in conn.execute(
            "SELECT name, agent, hits, last_hit FROM skills WHERE hits>0 "
            "ORDER BY hits DESC, last_hit DESC LIMIT 10")]
        zero = [dict(r) for r in conn.execute(
            "SELECT name, agent, path FROM skills WHERE hits=0 ORDER BY agent, name LIMIT 30")]
    return {"total": total, "by_agent": by_agent, "top_used": top, "zero_used": zero}


# ---------------------------------------------------------------- 导出/备份（X3）

def export_all(root: str, fmt: str = "markdown", limit: int = 5000) -> tuple:
    """X3 全库导出：markdown（人读：按项目分组的记录+记忆+任务+信号+决策）或
    json（机读：关键表全量 dump）。写到 <root>/_hub/export/agenthub-时间戳.{md,json}，
    返回 (错误或 "", 路径)。只读，不动库。"""
    import json as _json
    fmt = (fmt or "markdown").strip().lower()
    if fmt not in ("markdown", "json"):
        return "fmt 只能是 markdown/json", ""
    out_dir = db_path(root).parent / "export"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"导出目录创建失败：{e}", ""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    path = out_dir / f"agenthub-{stamp}.{'md' if fmt == 'markdown' else 'json'}"
    with db_conn(root) as conn:
        if fmt == "json":
            data = {"exported": _now(), "root": root}
            for tbl in ("records", "memories", "tasks", "signals", "checkpoints",
                        "decision_reviews", "projects", "agents"):
                try:
                    data[tbl] = [dict(r) for r in conn.execute(
                        f"SELECT * FROM {tbl} ORDER BY id DESC LIMIT ?",
                        (max(1, min(int(limit or 5000), 50000)),))]
                except sqlite3.Error:
                    data[tbl] = []
            path.write_text(_json.dumps(data, ensure_ascii=False, indent=1),
                            encoding="utf-8")
            return "", str(path)
        lines = [f"# AgentHub 导出（{_now()}）", "", f"根目录：{root}", ""]
        try:
            recs = [dict(r) for r in conn.execute(
                "SELECT * FROM records WHERE " + _rec_live_sql()
                + " ORDER BY project, id LIMIT ?",
                (max(1, min(int(limit or 5000), 50000)),))]
        except sqlite3.Error:
            recs = []
        cur_proj = None
        for r in recs:
            if r["project"] != cur_proj:
                cur_proj = r["project"]
                lines += ["", f"## 项目：{cur_proj}", ""]
            lines.append(f"### {r['date']} {r['title']}（#{r['id']} · {r['agent'] or '未署名'}）")
            if r["content"]:
                lines.append(r["content"])
            lines.append("")
        try:
            mems = [dict(r) for r in conn.execute(
                "SELECT * FROM memories WHERE " + _live_sql() + " ORDER BY kind, id")]
        except sqlite3.Error:
            mems = []
        lines += ["", "## 记忆", ""]
        for m in mems:
            lines.append(f"- [#{m['id']} {m['kind']}/{m['maturity']}] {m['content']}")
        try:
            tks = [dict(r) for r in conn.execute(
                "SELECT * FROM tasks WHERE status!='done' ORDER BY priority, id")]
        except sqlite3.Error:
            tks = []
        lines += ["", "## 未完成任务", ""]
        for t in tks:
            lines.append(f"- [#{t['id']} P{t['priority']} {t['status']}] {t['title']}"
                         f"（{t['project'] or '无项目'}）")
        path.write_text("\n".join(lines), encoding="utf-8")
        return "", str(path)


def backup_db(root: str, dest: str = "") -> tuple:
    """X3 备份：① 先走 sqlite3 官方 backup API 取**含 WAL 的一致性库快照**（**不用
    shutil 复制文件**——WAL 未 checkpoint 时 copy 出的库会丢数据，2026-10-12 实测假备份）；
    ② 再把这个一致库 + 整个磁盘项目文件树打包成单个 .zip（排除运营目录 backup/__pycache__/.git
    与库文件本体）。单一还原单元，避免「只备库、还原丢源文件」。dest 可指定目录（不存在则建）。
    返回 (错误或 "", zip 路径)。旧 .db 备份仍可被 backup_verify/backup_list 识别。"""
    import zipfile
    src = db_path(root)
    if not src.exists():
        return "源库不存在", ""
    out_dir = Path(dest).expanduser() if dest else db_path(root).parent / "backup"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"备份目录创建失败：{e}", ""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    zip_path = out_dir / f"brain-{stamp}.zip"
    tmp = out_dir / f".tmp-brain-{stamp}.db"
    s_conn = d_conn = None
    try:
        # ① 一致性库快照（含 WAL）
        s_conn = sqlite3.connect(str(src), timeout=5)
        d_conn = sqlite3.connect(str(tmp), timeout=5)
        s_conn.backup(d_conn)
        d_conn.close()
        s_conn.close()
        # ② 打包：库快照 + 磁盘文件树
        root_p = Path(root)
        EXCLUDE_DIRS = {"backup", "__pycache__", ".git"}
        EXCLUDE_FILES = {src.name, src.name + "-wal", src.name + "-shm", tmp.name}
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(tmp, arcname="brain.db")  # 一致库放根
            n_files = 0
            for p in root_p.rglob("*"):
                if p.is_dir():
                    continue
                rel = p.relative_to(root_p)
                if any(part in EXCLUDE_DIRS for part in rel.parts):
                    continue
                if rel.name in EXCLUDE_FILES:
                    continue
                zf.write(p, arcname=str(rel).replace("\\", "/"))
                n_files += 1
        tmp.unlink(missing_ok=True)
    except (sqlite3.Error, OSError, zipfile.BadZipFile) as e:
        for c in (d_conn, s_conn):
            try:
                if c:
                    c.close()
            except sqlite3.Error:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
        return f"备份失败：{e}", ""
    return "", str(zip_path)


def backup_list(root: str) -> list:
    """X3 备份清单（新在前）：路径/大小/修改时间/kind（zip=库+文件，db=仅库）。"""
    d = db_path(root).parent / "backup"
    if not d.exists():
        return []
    out = []
    for p in list(d.glob("brain-*.db")) + list(d.glob("brain-*.zip")):
        st = p.stat()
        out.append({"path": str(p), "size": st.st_size,
                    "mtime": datetime.datetime.fromtimestamp(
                        st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "kind": "zip" if p.suffix == ".zip" else "db"})
    out.sort(key=lambda x: x["path"], reverse=True)
    return out


def backup_verify(root: str, path: str) -> tuple:
    """X3 校验备份：只读打开 + 关键表计数（不写、不恢复——恢复是人的手工动作）。
    支持 .zip（库+文件，逐项校验完整性并报告文件数）与旧 .db（仅库）。
    返回 (错误或 "", 摘要)。"""
    import zipfile
    import tempfile
    p = Path(path).expanduser()
    if not p.exists():
        return f"备份文件不存在：{p}", ""
    try:
        if p.suffix == ".zip":
            with zipfile.ZipFile(p) as zf:
                if zf.testzip() is not None:
                    return "压缩包损坏（testzip 失败）", ""
                names = zf.namelist()
                if "brain.db" not in names:
                    return "备份缺少 brain.db（不是有效的 AgentHub 备份）", ""
                data = zf.read("brain.db")
            tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tf.write(data)
            tf.close()
            try:
                conn = sqlite3.connect(f"file:{tf.name}?mode=ro", uri=True, timeout=5)
                got = {}
                for tbl in ("records", "memories", "projects"):
                    try:
                        got[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                    except sqlite3.Error:
                        got[tbl] = -1
                conn.close()
            finally:
                try:
                    Path(tf.name).unlink(missing_ok=True)
                except OSError:
                    pass
            n_files = sum(1 for n in names if n != "brain.db")
            return "", ("备份可读：DB(" + " · ".join(f"{k} {v}" for k, v in got.items())
                        + f") · 文件 {n_files} 个")
        # 旧 .db 备份（仅库）
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        got = {}
        for tbl in ("records", "memories", "projects"):
            try:
                got[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except sqlite3.Error:
                got[tbl] = -1
        conn.close()
    except (sqlite3.Error, zipfile.BadZipFile, OSError) as e:
        return f"读取备份失败：{e}", ""
    return "", "备份可读：" + " · ".join(f"{k} {v}" for k, v in got.items())


# ---------------------------------------------------------------- 时间机器（X4）

def history_of(root: str, kind: str = "record", oid=0) -> dict:
    """X4 时间机器：某条记录/记忆的完整变更史（三源合并，按时间升序）。
    源：①journal（target 含 records#N / memories#N 的动作流水）②supersede 链
    （前驱/后继：谁取代了它、它取代了谁）③自身关键时点（创建/失效/取代/最后唤起）。
    返回 {"kind","id","item","timeline":[{"ts","source","event","detail"}]}。"""
    kind = (kind or "record").strip()
    if kind not in ("record", "memory"):
        return {"kind": kind, "id": oid, "item": None, "timeline": []}
    try:
        oid = int(oid)
    except (TypeError, ValueError):
        return {"kind": kind, "id": oid, "item": None, "timeline": []}
    table = "records" if kind == "record" else "memories"
    mark = "records" if kind == "record" else "memories"
    tl: list = []
    with db_conn(root) as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (oid,)).fetchone()
        item = dict(row) if row else None
        if not item:
            return {"kind": kind, "id": oid, "item": None, "timeline": []}
        # ① journal 流水（target 两种写法都认：records#N 与 brain:records#N）
        for r in conn.execute(
                "SELECT ts, action, agent, target, note FROM journal WHERE target LIKE ? "
                "ORDER BY id", (f"%{mark}#{oid}",)):
            tl.append({"ts": r["ts"], "source": "journal",
                       "event": r["action"], "detail": (r["note"] or "")[:120]})
        # ② supersede 链
        if item.get("superseded_by"):
            tl.append({"ts": item.get("updated") or "", "source": "supersede",
                       "event": "被取代", "detail": f"→ #{item['superseded_by']}（仍可追溯）"})
        # 摘要列两表不同（records 有 title；memories 只有 content——同 M8 的坑）
        tcol = "title" if table == "records" else "substr(content,1,40) AS title"
        for r in conn.execute(
                f"SELECT id, {tcol} FROM {table} WHERE superseded_by=?", (oid,)):
            tl.append({"ts": "", "source": "supersede", "event": "取代了",
                       "detail": f"→ #{r['id']} {(r['title'] or '')[:40]}"})
        # ③ 自身关键时点
        if item.get("created"):
            tl.append({"ts": item["created"], "source": "item", "event": "创建",
                       "detail": f"by {item.get('agent') or '未署名'}"})
        if item.get("valid_until"):
            tl.append({"ts": item["valid_until"], "source": "item", "event": "失效期止",
                       "detail": "过期后退出检索（失效≠删除）"})
        if item.get("last_hit"):
            tl.append({"ts": item["last_hit"], "source": "item", "event": "最后唤起",
                       "detail": f"累计 {item.get('use_count') or 0} 次"})
        if item.get("status") != "active":
            tl.append({"ts": item.get("updated") or "", "source": "item",
                       "event": f"状态 {item['status']}", "detail": "非 active=退出检索"})
    tl.sort(key=lambda x: x["ts"] or "")
    return {"kind": kind, "id": oid, "item": item, "timeline": tl}


def heartbeat_touch(root: str, agent: str, project: str = "", note: str = "") -> tuple:
    """更新心跳并重建会话表（清掉超出 SESSION_RETAIN 的条目），返回 (错误或"", 同项目活跃会话)。

    注意「剪枝窗口 ≠ 在线窗口」：剪枝按 SESSION_RETAIN（24h，服务展示），
    返回的 active 按 SESSION_ACTIVE（5 分钟，同项目正在工作的并发判定）——两者互不影响。"""
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
            # 2026-10-11：此处原用 SESSION_TTL（30 分钟在线窗口）→ 会话记录活不过 30 分钟，
            # 展示层再放宽也读不到；改用独立的保留窗口 SESSION_RETAIN。
            if (now - ts).total_seconds() < SESSION_RETAIN:
                fresh.append(s)
        conn.execute("DELETE FROM sessions")  # 重建语义：超出保留窗口者一并清除
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


def active_sessions(root: str, ttl: int = SESSION_TTL) -> list:
    """当前活跃会话（最后一跳心跳在 ttl 秒内）。缺省 SESSION_TTL=「在线」语义（供 hub_status/
    list_agents 打 live 标）；GUI 总览可传大窗口（24h）展示「最近活动」——只放宽展示，
    不改在线判定。2026-10-11 修复：GUI 曾用缺省 30 分钟窗口，正常使用中常显空。"""
    now = datetime.datetime.now()
    with db_conn(root) as conn:
        out = []
        for s in conn.execute("SELECT * FROM sessions"):
            d = dict(s)
            if _ts_age(d.get("ts", ""), now) < ttl:
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


# ---------------------------------------------------------------- 连续性层（3.1）

STATUS_ACTIVE_DAYS = 7  # 项目"还活跃"窗口（天）：与 90 天 stalled 判定构成两端


def status_snapshot(root: str) -> dict:
    """此刻的活状态快照（ATANT 五问：什么活跃/什么变了/什么还开着/什么还重要/下一步）。
    只读不写——状态查询不进 searches 表，避免高频调用污染读写比口径。"""
    now = datetime.datetime.now()
    today = now.date().isoformat()
    cutoff = (now - datetime.timedelta(days=STATUS_ACTIVE_DAYS)).date().isoformat()
    out = {"now": now.isoformat(timespec="minutes"), "today": today,
           "sessions": active_sessions(root)}
    with db_conn(root) as conn:
        out["active_projects"] = [dict(r) for r in conn.execute(
            "SELECT project AS name, MAX(date) AS last_date, COUNT(*) AS n FROM records "
            "WHERE status='active' AND date>=? GROUP BY project "
            "ORDER BY last_date DESC, n DESC LIMIT 5", (cutoff,))]
        out["records_today"] = conn.execute(
            "SELECT COUNT(*) FROM records WHERE substr(created,1,10)=?", (today,)).fetchone()[0]
        out["memories_today"] = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE substr(created,1,10)=?", (today,)).fetchone()[0]
        out["journal"] = [dict(r) for r in conn.execute(
            "SELECT ts, agent, action, note FROM journal ORDER BY id DESC LIMIT 8")]
        out["errors_open"] = [dict(r) for r in conn.execute(
            "SELECT id, agent, title FROM errors WHERE status='open' ORDER BY id DESC LIMIT 5")]
        out["errors_open_total"] = conn.execute(
            "SELECT COUNT(*) FROM errors WHERE status='open'").fetchone()[0]
        out["handoffs"] = [dict(r) for r in conn.execute(
            "SELECT id, project, agent FROM handoffs WHERE status='open' ORDER BY id DESC LIMIT 5")]
        out["handoffs_total"] = conn.execute(
            "SELECT COUNT(*) FROM handoffs WHERE status='open'").fetchone()[0]
        out["pinned"] = [dict(r) for r in conn.execute(
            "SELECT id, kind, substr(content,1,50) AS content FROM memories "
            "WHERE status='active' AND pinned=1 ORDER BY id LIMIT 5")]
        out["stalled"] = [r["name"] for r in conn.execute(
            "SELECT name FROM projects WHERE status='stalled' ORDER BY name LIMIT 5")]
        out["stalled_total"] = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE status='stalled'").fetchone()[0]
    out["todos"] = extract_todos(root, limit=5)
    return out


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


_OUTLINE_HEAD_RE = re.compile(r"^(#{1,4}\s+\S|\*\*[^*]{2,30}\*\*|【[^】]{1,14}】)")
_DECISION_HEAD_RE = re.compile(r"^(#{1,4}\s*决策记录|【决策记录】|\*\*决策记录\*\*)")


def record_outline(content: str) -> str:
    """outline 级骨架：抽段头行（markdown 标题/加粗段头/【】段头），最多四段；
    四段式记录的段头行自带同行摘要，无段头结构的记录退化为前 4 行。
    决策段头（3.2 AgDR）必含——四段式正好占满四个槽位，决策段按常规抽取会被挤掉，
    故抽到即追加（最多 4+1 行）。
    渐进披露的第二级——比 full 便宜一个量级，比 abstract 多结构。"""
    lines = [l for l in (content or "").splitlines() if l.strip()]
    picked = [l.strip()[:100] for l in lines if _OUTLINE_HEAD_RE.match(l.strip())][:4]
    if not picked:
        picked = [l.strip()[:100] for l in lines[:4]]
    for l in lines:
        if _DECISION_HEAD_RE.match(l.strip()):
            dl = l.strip()[:100]
            if dl not in picked:
                picked.append(dl)
            break
    return "\n".join(picked)


def get_record(root: str, record_id: int, level: str = "full") -> dict:
    """单条记录读取（渐进披露三级）：hub_search 行 → abstract/outline → full，
    每级成本差一个量级、每一级都是一个可停下的地方。
    level：full=全文（缺省，与旧版完全一致）；abstract=正文前 200 字；outline=段头骨架。
    非法 level 一律按 full；坏 id 一律 {}。"""
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
    d = dict(row) if row else {}
    if d:
        if level == "abstract":
            d["content"] = (d["content"] or "").strip()[:200]
        elif level == "outline":
            d["content"] = record_outline(d["content"])
    return d


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
            "SELECT id, kind, content FROM memories WHERE " + _live_sql() + " ORDER BY id")]
    toks = [(r["id"], r["kind"], r["content"], _tokens(r["content"])) for r in rows]
    out = []
    for i in range(len(toks)):
        ta = toks[i][3]
        if not ta:
            continue              # 空词集：与任何 j 的 sim 恒为 0，整行跳过（等价，省一层）
        sa = len(ta)
        for j in range(i + 1, len(toks)):
            tb = toks[j][3]
            if not tb:
                continue
            sb = len(tb)
            # Jaccard 上界短路（B5）：|A∩B| <= min(|A|,|B|) 且 |A∪B| >= max(|A|,|B|)
            # → sim <= min/max。上界够不到阈值就不必做 set 交/并——那是 O(N²) 里最贵的一步，
            # 也是 hub_metacognition（dup_pairs）在大库上的主开销。只跳「数学上不可能达阈值」
            # 的对，结果集合与迭代顺序完全不变（可回归比对）。
            lo, hi = (sa, sb) if sa < sb else (sb, sa)
            if lo / hi < threshold:
                continue
            inter = len(ta & tb)
            # |A∪B| = |A|+|B|-|A∩B|：不必构造并集（省一次 O(|A|+|B|) 建集，纯等价代数替换）
            sim = inter / (sa + sb - inter)
            if sim >= threshold:
                out.append({"a": toks[i][0], "b": toks[j][0], "sim": round(sim, 2),
                            "verdict": _verdict(toks[i][2], toks[j][2], toks[i][1] == toks[j][1]),
                            "content_a": toks[i][2][:80], "content_b": toks[j][2][:80]})
            if len(out) >= max(1, min(limit, 50)):
                return out
    out.sort(key=lambda x: -x["sim"])
    return out


def similar_lessons_for(root: str, content: str, threshold: float = 0.15, limit: int = 3) -> list:
    """写入时踩坑拦截：新记录 content 的 bigram 词集 vs 库内 lesson/fact 记忆与 open 错误登记，
    Jaccard ≥ threshold 视为"可能正在重蹈已记录的坑"。阈值 0.15 为真实库实测标定：
    同源记录-记忆对（真相关）0.21~0.33，异表述同主题（手写重放）0.17~0.19，真无关 ≤0.08；
    记录长记忆短导致并集偏大、相似度天然偏低，拦截宁可多提醒（agent 看一眼自行取舍），
    漏报代价高于误报。返回 [{id, kind, sim, content}] 按 sim 降序，kind=error 表示 open 错误登记。"""
    toks = _tokens(content)
    if len(toks) < 4:
        return []
    with db_conn(root) as conn:
        rows = [{"id": r["id"], "kind": r["kind"], "content": r["content"]} for r in conn.execute(
            "SELECT id, kind, content FROM memories WHERE " + _live_sql() + " AND kind IN ('lesson','fact')")]
        rows += [{"id": -r["id"], "kind": "error", "content": f"{r['title']} {r['detail']}"}
                 for r in conn.execute("SELECT id, title, detail FROM errors WHERE status='open'")]
    out = []
    for r in rows:
        ot = _tokens(r["content"])
        if not ot:
            continue
        sim = len(toks & ot) / len(toks | ot)
        if sim >= threshold:
            out.append({"id": r["id"], "kind": r["kind"], "sim": round(sim, 2),
                        "content": r["content"][:80]})
    out.sort(key=lambda x: -x["sim"])
    return out[:max(1, min(limit, 5))]


def mark_intercept_hit(root: str, agent: str, rec_id: int, hits: list) -> None:
    """拦截命中落痕（2026-10-03 回放排查：上线首日 2 条本应命中但零观测——
    返回给 agent 看一眼就丢，journal 不落、use_count 不计，验收②无法举证）。
    落两笔：流水记一条"拦截命中" + 命中记忆 use_count+1（错误登记负 id 只记流水）。
    观测不能打断写入主链路，任何失败静默。"""
    try:
        hits = hits or []
        if not hits:
            return
        note = " ".join(
            (f"错误登记#{-h['id']}" if h["id"] < 0 else f"记忆#{h['id']}") + f"@{h['sim']}"
            for h in hits)
        # 直接落 db journal 表（勿走 core.journal——MCP 进程无 SINK 时写文件，wakeups
        # 的 ran_today/流水页查的都是 db 表，两边必须同源）
        journal_add(root, agent or "unknown", "拦截命中",
                    target=f"records#{rec_id}", note=note[:200])
        with db_conn(root) as conn:
            for h in hits:
                if h["id"] > 0:
                    conn.execute("UPDATE memories SET use_count=use_count+1, last_hit=?, "
                                 "maturity=CASE WHEN pinned=1 AND use_count+1>=? THEN 'core' "
                                 "WHEN maturity='draft' AND use_count+1>=? THEN 'validated' "
                                 "ELSE maturity END WHERE id=?",
                                 (_now(), CORE_AT, VALIDATE_AT, h["id"]))
    except Exception:  # noqa: BLE001 — 观测失败任何原因都静默，不能打断 log_work 主链路
        pass


def archive_project(root: str, name: str, agent: str = "") -> str:
    """项目归档（清理决策的落地动作）：状态改 archived（不再计入活跃/stalled）；
    目录存在则移入 <root>/99_Archive\\（可逆：移回 + 状态改回 active 即恢复）。
    add_record 对 archived 项目有 status!='archived' 守卫——归档后新记录不会误复活它。"""
    import shutil
    name = (name or "").strip()
    if not name:
        return "project 必填"
    with db_conn(root) as conn:
        row = conn.execute("SELECT name, status FROM projects WHERE name=?", (name,)).fetchone()
        if not row:
            return f"未找到项目：{name}"
        if row["status"] == "archived":
            return f"项目已是归档状态：{name}"
        conn.execute("UPDATE projects SET status='archived' WHERE name=?", (name,))
    src = Path(root) / name
    if src.is_dir():
        dst_dir = Path(root) / core.DIR_ARCHIVE
        dst = dst_dir / name
        try:
            dst_dir.mkdir(exist_ok=True)
            if dst.exists():
                return f"状态已归档，但 99_Archive\\{name} 已存在同名目录，目录未动（请手动处理）"
            shutil.move(str(src), str(dst))
        except OSError as e:
            return f"状态已归档，但目录移动失败（可手动移入 99_Archive）：{e}"
    return ""


def health_report(root: str) -> dict:
    """大脑体检：汇总各智能维度 + 数据规模，供 hub_health 工具与 GUI 体检摘要。"""
    s = stats(root)
    todos = extract_todos(root, limit=50)
    sims = similar_memories(root, limit=20)
    with db_conn(root) as conn:
        bare_titles = conn.execute(
            "SELECT COUNT(*) FROM records WHERE status='active' "
            "AND title GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]（*）'").fetchone()[0]
        push_total = conn.execute(
            "SELECT COUNT(*) FROM searches WHERE tool='recall_push'").fetchone()[0]
        push_by_agent = {r[0] or "未知": r[1] for r in conn.execute(
            "SELECT agent, COUNT(*) FROM searches WHERE tool='recall_push' GROUP BY agent ORDER BY 2 DESC")}
        search_by_agent = {r[0] or "未知": r[1] for r in conn.execute(
            "SELECT agent, COUNT(*) FROM searches WHERE tool!='recall_push' GROUP BY agent ORDER BY 2 DESC")}
        tool_breakdown = {r[0]: r[1] for r in conn.execute(
            "SELECT tool, COUNT(*) FROM searches GROUP BY tool ORDER BY 2 DESC")}
        top_pushed = [dict(r) for r in conn.execute(
            "SELECT id, kind, content, use_count, last_hit FROM memories "
            "WHERE status='active' AND use_count>0 ORDER BY use_count DESC, id DESC LIMIT 10")]
        archived = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE status='archived'").fetchone()[0]
        # 验收达成度（2026-09-30 定四条可验证标准，2026-10-14 复查）
        cross_agents = [r[0] for r in conn.execute(
            "SELECT DISTINCT agent FROM searches WHERE tool!='recall_push' AND agent!='' ORDER BY agent")]
        intercept_hits = conn.execute(
            "SELECT COUNT(*) FROM journal WHERE action LIKE '拦截命中%'").fetchone()[0]
        stalled_used = conn.execute(
            "SELECT COUNT(*) FROM journal WHERE action LIKE '检查 stalled%' OR action LIKE '%archive%'").fetchone()[0]
        pinned_total, pinned_work = conn.execute(
            "SELECT COUNT(*), IFNULL(SUM(CASE WHEN kind IN ('lesson','fact','project') THEN 1 ELSE 0 END),0) "
            "FROM memories WHERE " + _live_sql() + " AND pinned=1").fetchone()
        # M6 成熟度分布（信任分层）：draft 未验证 / validated 用过或人工确认 / core 核心
        maturity_dist = {r[0] or "draft": r[1] for r in conn.execute(
            "SELECT maturity, COUNT(*) FROM memories WHERE " + _live_sql() + " GROUP BY maturity")}
        for lv in MATURITY_LEVELS:
            maturity_dist.setdefault(lv, 0)
        # 记忆保鲜：lesson/fact 写入超 90 天且从未唤起/长期未唤起——环境漂移后可能已失真
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=90)).isoformat(timespec="seconds")
        stale_rows = conn.execute(
            "SELECT id, kind, substr(content,1,60) AS content FROM memories WHERE " + _live_sql() + " "
            "AND kind IN ('lesson','fact') AND created < ? "
            "AND (last_hit IS NULL OR last_hit < ?) ORDER BY id LIMIT 5", (cutoff, cutoff)).fetchall()
        # 诚实指标（0.5）：数据健康度而非能力指标——先诚实，再谈能力（不照抄 LongMemEval/LOCOMO）
        mem_total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE " + _live_sql() + "").fetchone()[0]
        mem_used = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE " + _live_sql() + " AND use_count>0").fetchone()[0]
        mem_meta = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE " + _live_sql() + " "
            "AND (content LIKE '%AgentHub%' OR content LIKE '%hub\\_%' ESCAPE '\\' "
            "     OR content LIKE '%大脑%')").fetchone()[0]
        rec_total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        srch_total = conn.execute(
            "SELECT COUNT(*) FROM searches WHERE tool!='recall_push'").fetchone()[0]
        # 读写比分母须同期同口径（0.5 ⚠️）：剔除迁移存量（meta.records_migrated_at 由 0.1 锚定），
        # 否则 88 条时代新增 vs 全部 1523 条差 17 倍，比率全失真；无锚点时回退全部记录口径
        rec_era, era_note = rec_total, "全部记录"
        _mig_at = conn.execute("SELECT value FROM meta WHERE key='records_migrated_at'").fetchone()
        if _mig_at and _mig_at[0]:
            migrated_n = conn.execute(
                "SELECT COUNT(*) FROM records WHERE substr(created,1,10)=?",
                (_mig_at[0],)).fetchone()[0]
            rec_era = max(0, rec_total - migrated_n)
            era_note = f"仅时代新增（剔除 {_mig_at[0]} 迁移存量 {migrated_n} 条）"
        _since = conn.execute("SELECT value FROM meta WHERE key='active_recall_since'").fetchone()
        since_txt = _since[0] if _since else "尚无主动检索记录"
        # 冷记忆可见化（0.3）：衰减分最低在前，只展示不动作——衰减从不参与检索排序
        now_dt = datetime.datetime.now()
        cold_rows = [dict(r) for r in conn.execute(
            "SELECT id, kind, substr(content,1,60) AS content, created, use_count, last_hit, salience "
            "FROM memories WHERE " + _live_sql() + " AND pinned=0 ORDER BY id LIMIT 500")]
        cold = [r for r in cold_rows if is_cold(r, now_dt)]
        for r in cold:
            r["retention"] = round(retention_score(r, now_dt), 4)
        cold.sort(key=lambda r: r["retention"])
    return {"records": s["records"], "memories": s["memories"], "projects": s["projects"],
            "projects_stalled": s.get("projects_stalled", 0), "projects_archived": archived,
            "errors_open": s.get("errors_open", 0),
            "searches_today": s.get("searches_today", 0), "searches_total": s.get("searches_total", 0),
            "todos": todos, "todo_count": len(todos),
            "dup_memories": sims, "dup_memory_count": len(sims),
            # 疑似互斥对（2026-10-13）：一条像在修正另一条——比"疑似重复"更危险，
            # 重复只是浪费上下文，互斥会让 agent 拿矛盾的事实干活。只报数不仲裁。
            "conflict_pairs": [x for x in sims if "矛盾" in (x.get("verdict") or "")],
            "conflict_pair_count": sum(1 for x in sims if "矛盾" in (x.get("verdict") or "")),
            "bare_titles": bare_titles,
            "recall_push_total": push_total, "push_by_agent": push_by_agent,
            "search_by_agent": search_by_agent, "tool_breakdown": tool_breakdown,
            "top_pushed": top_pushed,
            "acceptance": {"cross_agent_searches": cross_agents, "intercept_hits": intercept_hits,
                           "stalled_used": stalled_used, "pinned_total": pinned_total,
                           "pinned_work": pinned_work},
            "maturity_dist": maturity_dist,
            "stale_memories": [dict(r) for r in stale_rows],
            "cold_memories": cold[:10], "cold_count": len(cold),
            "honest": {
                "memories_total": mem_total, "memories_used": mem_used,
                "memory_use_rate": round(mem_used * 100 / max(1, mem_total), 1),
                "meta_memories": mem_meta,
                "meta_ratio": round(mem_meta * 100 / max(1, mem_total), 1),
                "read_write_ratio": round(srch_total / max(1, rec_era), 2),
                "read_write_records_total": rec_era,
                "note": (f"use_count 自 {since_txt} 起才同时统计主动检索，此前只含被动推送；"
                         f"读写比分母口径：{era_note}"),
            },
            "mem_governance": memory_governance_stats(root),
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


# 用户画像技术栈词表（依据 AGENTS.md 技术栈速查；长词在前防子串重复计数）
_STACK_WORDS = ["python-docx", "python-pptx", "pyinstaller", "qfluentwidgets", "pyside6",
                "matplotlib", "flask", "ffmpeg", "matlab", "python", "sqlite",
                "powershell", "git", "everything"]


def user_profile(root: str) -> dict:
    """用户画像（3.3 用户建模，零 LLM）：只做实测统计与记忆原文引用，不生成推断结论。

    口径随行注记——时段样本=journal 操作流水（v2 起才有）；技术栈=记录正文词频出现；
    单日项目口径与 report._sec_time 一致。preference 计数单列：为 0 时如实提示
    「偏好未结构化」，这是蒸馏的行动信号而非错误。
    """
    with db_conn(root) as conn:
        kinds = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM memories WHERE " + _live_sql() + " GROUP BY kind"))
        hours = conn.execute(
            "SELECT substr(ts,12,2) hh, COUNT(*) n FROM journal "
            "WHERE ts GLOB '????-??-??T*' GROUP BY hh").fetchall()
        agents = {r[0] or "其他": r[1] for r in conn.execute(
            "SELECT agent, COUNT(*) n FROM records WHERE status='active' "
            "GROUP BY agent ORDER BY n DESC LIMIT 8")}
        n_records = conn.execute(
            "SELECT COUNT(*) FROM records WHERE status='active'").fetchone()[0]
        n_projects = conn.execute(
            "SELECT COUNT(DISTINCT project) FROM records WHERE status='active'").fetchone()[0]
        oneday = conn.execute(
            "SELECT COUNT(*) FROM (SELECT project FROM records WHERE status='active' "
            "GROUP BY project HAVING COUNT(DISTINCT date)=1)").fetchone()[0]
        lessons = [dict(r) for r in conn.execute(
            "SELECT id, content, use_count FROM memories "
            "WHERE status='active' AND kind='lesson' ORDER BY use_count DESC, id DESC LIMIT 8")]
        texts = [r[0] for r in conn.execute("SELECT content FROM records WHERE status='active'")]
    bands = {"凌晨(0-6)": 0, "上午(6-12)": 0, "下午(12-18)": 0, "晚间(18-24)": 0}
    keys = list(bands)
    for hh, n in hours:
        try:
            bands[keys[min(int(hh) // 6, 3)]] += n
        except (ValueError, TypeError):
            continue  # 畸形小时位直接不计入
    n_journal = sum(bands.values())  # 样本=有效时段样本（畸形 ts 不计入）
    low = "\n".join(texts).lower()
    stack = sorted(((w, low.count(w)) for w in _STACK_WORDS if low.count(w)), key=lambda t: -t[1])
    return {
        "kinds": kinds,
        "preference_n": kinds.get("preference", 0),
        "time_bands": bands,
        "time_sample": n_journal,
        "agents": agents,
        "records": n_records,
        "oneday_pct": round(oneday * 100 / max(1, n_projects), 1),
        "stack": stack[:8],
        "lessons": lessons,
    }


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
    """记忆蒸馏候选（结晶流水线）：content 含「目的」结论但同项目无相似记忆覆盖、
    且往轮 hub_distill 未展示过的记录。行业头部（mem0/腾讯AgentMemory）用 LLM 蒸馏；
    本方案零成本规则版——目的行提取 + bigram 相似度查重，候选经 agent/用户确认后
    hub_memory_write 沉淀为记忆。只读不标记：登记在 mark_distill_shown（消费侧）。"""
    with db_conn(root) as conn:
        recs = [dict(r) for r in conn.execute(
            "SELECT id, project, date, agent, content FROM records "
            "WHERE status='active' AND id NOT IN "
            "(SELECT record_id FROM distill_seen) ORDER BY id DESC LIMIT 300")]
        mems = [dict(r) for r in conn.execute(
            "SELECT project, content FROM memories WHERE " + _live_sql() + "")]
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
            # 元信息标注（v2.8.5）：「用户拍板/执行/继续」开头的目的行是操作记录不是工作知识，
            # 沉淀价值低（结晶率虚高的元信息霸榜，2026-10-03 实测 5 候选全元信息），标注供取舍
            meta = bool(re.match(r"^(用户|执行|继续|批准|复盘)", gist))
            out.append({"id": r["id"], "project": r["project"], "date": r["date"],
                        "agent": r["agent"], "gist": "；".join(uncovered)[:80], "meta": meta})
        if len(out) >= max(1, min(limit, 50)):
            return out
    return out


def mark_distill_shown(root: str, record_ids: list) -> int:
    """蒸馏候选展示即登记（hub_distill 消费侧调用；GUI 只读计数不标记）。
    已展示的记录不再进候选——空壳目的行（如"执行用户指令xx"）与沉淀进记忆的
    实际内容文字不重叠，纯内容查重永远排除不掉，会反复霸榜（2026-10-02 实测
    #2021/#2022 已蒸馏过仍每次出现）。幂等，返回本次新登记条数。"""
    if not record_ids:
        return 0
    with db_conn(root) as conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO distill_seen(record_id, ts) VALUES (?, ?)",
            [(int(i), _now()) for i in record_ids])
        return cur.rowcount


# ==================================================================== Y1 编排层（Playbook 引擎）
# 由来（真实痛点）：发布、新项目开工、每日巡检、交付收尾这类流程，每次都由 agent 凭记忆
# 手工重走一遍——步骤散落在各家提示词里，走没走全、走到第几步崩了无人知晓，换个会话就断。
# 这里把流程固化成**可执行**的 playbook：步骤落库、逐步真跑、失败即停、断点续跑。
# 大脑从「记流程」进化到「跑流程」——这是编排层，不是又一个待办清单。

PB_VAR_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")

# 内置模板：{{project}}/{{agent}}/{{today}} 运行时替换，{{out.步骤key}} 引用前序步骤输出
_BUILTIN_PLAYBOOKS = [
    {"name": "release", "title": "发布全流程",
     "desc": "改完 AgentHub 源码后跑：环境预检→语法检查→部署一致性→exe 新旧→备份→留痕。失败即停，修完 resume 接着跑",
     "steps": [
         {"key": "env", "title": "环境预检", "action": "ops_check", "args": {"name": "env"}},
         {"key": "syntax", "title": "语法检查", "action": "ops_check", "args": {"name": "py_compile"}},
         # 回归（7 套约 90 秒）放在部署之前：编译过不代表没改坏行为，早点失败早点停。
         # 耗时由断点续跑兜住——真超时了 resume 从这一步继续，不重跑前面已过的步骤。
         {"key": "regression", "title": "回归测试（7 套，约 90 秒）", "action": "ops_check",
          "args": {"name": "regression"}},
         {"key": "deploy", "title": "部署一致性（源码 vs ~/.agenthub）", "action": "ops_check",
          "args": {"name": "deploy_diff"}},
         {"key": "exe", "title": "exe 是否比源码旧", "action": "ops_check", "args": {"name": "release"}},
         {"key": "backup", "title": "备份大脑（库+文件）", "action": "backup", "args": {}},
         {"key": "log", "title": "落一条发布记录", "action": "log_work",
          "args": {"title": "发布流程自动执行",
                   "content": "playbook release 执行结果：语法={{out.syntax}}｜部署={{out.deploy}}｜exe={{out.exe}}｜备份={{out.backup}}"}},
     ]},
    {"name": "new_project", "title": "新项目开工",
     "desc": "建目录→拉开工记录→关注该项目。项目名用 {{project}}，运行时传入",
     "steps": [
         {"key": "create", "title": "创建项目目录", "action": "create_project", "args": {"name": "{{project}}"}},
         {"key": "kickoff", "title": "写开工记录", "action": "log_work",
          "args": {"title": "项目开工", "content": "项目 {{project}} 开工（{{today}}，执行者 {{agent}}）"}},
         {"key": "follow", "title": "关注该项目动态", "action": "view_follow", "args": {"scope_value": "{{project}}"}},
     ]},
    {"name": "daily", "title": "每日巡检",
     "desc": "只读体检套餐：大脑健康→哨兵巡检→停滞项目→整洁度→Inbox 积压。全通过则安静",
     "steps": [
         {"key": "health", "title": "大脑体检", "action": "ops_check", "args": {"name": "brain"}},
         {"key": "db", "title": "数据库健康（WAL/并发/体积）", "action": "ops_check",
          "args": {"name": "db_health"}},
         {"key": "stdout", "title": "stdout 污染守卫（防 MCP 崩）", "action": "ops_check",
          "args": {"name": "stdout_guard"}},
         {"key": "drift", "title": "工具 schema 漂移（声明了没实现）", "action": "ops_check",
          "args": {"name": "schema_drift"}},
         {"key": "graph", "title": "图谱健康（边/孤岛/最大簇）", "action": "ops_check",
          "args": {"name": "graph_health"}},
         {"key": "date_health", "title": "日期格式健康（坏格式会让到期提醒静默失效）", "action": "ops_check",
          "args": {"name": "date_health"}},
         {"key": "sentry", "title": "哨兵巡检（gap 自动广播）", "action": "sentry", "args": {}},
         {"key": "stalled", "title": "停滞项目", "action": "ops_check", "args": {"name": "stalled"}},
         {"key": "hygiene", "title": "整洁体检", "action": "ops_check", "args": {"name": "hygiene"}},
         {"key": "inbox", "title": "Inbox 待分拣", "action": "ops_check", "args": {"name": "inbox"}},
     ]},
    {"name": "deliver", "title": "交付收尾",
     "desc": "干完活收尾：语法检查→部署一致性→备份→人工看蒸馏候选→落交付记录",
     "steps": [
         {"key": "syntax", "title": "语法检查", "action": "ops_check", "args": {"name": "py_compile"}},
         {"key": "deploy", "title": "部署一致性", "action": "ops_check", "args": {"name": "deploy_diff"}},
         {"key": "backup", "title": "备份大脑（库+文件）", "action": "backup", "args": {}},
         {"key": "distill", "title": "看一眼蒸馏候选并决定是否沉淀", "action": "manual",
          "note": "跑 hub_distill，有「目的」结论的用 hub_memory_write 沉淀（系统不替你判断价值）"},
         {"key": "log", "title": "落交付记录", "action": "log_work",
          "args": {"title": "交付收尾", "content": "{{project}} 收尾：语法={{out.syntax}}｜部署={{out.deploy}}"}},
     ]},
    {"name": "onboard", "title": "新 agent 接入体检",
     "desc": "核对谁还没接上大脑：接入状态→规则执行体检→备份",
     "steps": [
         {"key": "agents", "title": "各 agent 接入状态", "action": "ops_check", "args": {"name": "agents"}},
         {"key": "rules", "title": "规则执行体检（闸门是否真跑）", "action": "ops_check",
          "args": {"name": "rules_health"}},
         {"key": "backup", "title": "备份大脑（库+文件）", "action": "backup", "args": {}},
     ]},
]


def _default_src_dir() -> str:
    """需要 target 的 ops 步骤默认目录：brain.py 所在目录（= AgentHub 源码目录）。
    部署版（~/.agenthub/mcp_server）缺 adv_*.py 时回归步骤会明确报「不是源码目录」——宁可明说，不猜路径。"""
    return os.path.dirname(os.path.abspath(__file__))


def _pb_render(text, ctx) -> str:
    """{{var}} 替换；支持 {{out.步骤key}} 引用前序输出。未知变量原样保留（不静默吞成空串）。"""
    def _rep(m):
        key = m.group(1)
        if key.startswith("out."):
            return str((ctx.get("out") or {}).get(key[4:], ""))
        return str(ctx.get(key, m.group(0)))
    return PB_VAR_RE.sub(_rep, text or "")


def _pb_args(step: dict, ctx: dict) -> dict:
    """步骤 args → 渲染后的 dict（库里存 JSON 串，也容忍调用方直接传 dict）。"""
    raw = step.get("args")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except (ValueError, TypeError):
            raw = {}
    if not isinstance(raw, dict):
        return {}
    return {k: (_pb_render(v, ctx) if isinstance(v, str) else v) for k, v in raw.items()}


# ---------------------------------------------------------------- 动作注册表
# 每个动作 fn(root, args, ctx) -> (ok: bool, output: str)。全部本地执行、可失败、不吞异常。
def _act_ops_check(root, args, ctx):
    import ops  # noqa: PLC0415  延迟导入：ops 反向依赖 brain，模块级导入会成环
    name = str(args.get("name") or "env")
    target = str(args.get("target") or ctx.get("target") or "")
    try:
        r = ops.run_check(name, target, root)
    except Exception as _e:  # noqa: BLE001  单个动作炸了不能拖垮整轮，但要留痕
        return False, f"ops.run_check 异常：{type(_e).__name__}: {_e}"
    out = str(r.get("summary") or "")
    det = [str(d) for d in (r.get("detail") or [])[:2]]
    if det:
        out += " ｜ " + " / ".join(det)
    return bool(r.get("ok")), out[:500]


def _act_backup(root, args, ctx):
    err, path = backup_db(root, str(args.get("dest") or ""))
    return (not err), (str(path) if not err else f"备份失败：{err}")


def _act_export(root, args, ctx):
    try:
        lim = int(args.get("limit") or 5000)
    except (TypeError, ValueError):
        lim = 5000
    err, path = export_all(root, str(args.get("fmt") or "markdown"), lim)
    return (not err), (str(path) if not err else f"导出失败：{err}")


def _act_log_work(root, args, ctx):
    proj = str(args.get("project") or ctx.get("project") or "")
    if not proj:
        return False, "缺 project：playbook 运行时传 project，或在步骤 args 里写死"
    rid = add_record(root, proj, str(args.get("agent") or ctx.get("agent") or "unknown"),
                     ctx.get("today", ""), str(args.get("title") or "playbook 自动记录"),
                     _pb_render(str(args.get("content") or ""), ctx), source="auto")
    return True, f"已写工作记录 #{rid}"


def _act_memory(root, args, ctx):
    content = _pb_render(str(args.get("content") or ""), ctx)
    if not content.strip():
        return False, "memory 步骤缺 content"
    mid = add_memory(root, content, str(args.get("kind") or "note"), str(args.get("tags") or ""),
                     str(args.get("project") or ctx.get("project") or ""),
                     str(args.get("agent") or ctx.get("agent") or ""))
    return True, f"已写记忆 #{mid}"


def _act_signal(root, args, ctx):
    tip, sid = signal_send(root, str(args.get("from_agent") or ctx.get("agent") or "unknown"),
                           _pb_render(str(args.get("content") or ""), ctx),
                           str(args.get("to_agent") or ""), str(args.get("kind") or "msg"),
                           str(args.get("project") or ctx.get("project") or ""))
    return (sid > 0), str(tip)


def _act_create_project(root, args, ctx):
    import core  # noqa: PLC0415
    name = _pb_render(str(args.get("name") or ""), ctx)
    err = core.create_project(root, name)
    if err:
        return False, str(err)
    with db_conn(root) as conn:
        conn.execute("INSERT OR IGNORE INTO projects(name, created) VALUES(?,?)", (name, _now()))
    return True, f"项目已创建：{name}"


def _act_view_follow(root, args, ctx):
    err = view_follow(root, str(args.get("agent") or ctx.get("agent") or ""),
                      str(args.get("scope_type") or "project"),
                      _pb_render(str(args.get("scope_value") or ""), ctx))
    return (not err), (err or "已关注")


def _act_checkpoint(root, args, ctx):
    err, cid = checkpoint_save(root, args.get("task_id"),
                               str(args.get("agent") or ctx.get("agent") or ""),
                               _pb_render(str(args.get("content") or ""), ctx),
                               str(args.get("kind") or "manual"))
    return (not err), (f"检查点 #{cid}" if not err else str(err))


def _act_sentry(root, args, ctx):
    try:
        cooldown = int(args.get("cooldown_hours") or 24)
    except (TypeError, ValueError):
        cooldown = 24
    try:
        summary, sid = sentry_sweep(root, cooldown)
    except Exception as _e:  # noqa: BLE001
        return False, f"sentry 异常：{type(_e).__name__}: {_e}"
    return True, f"{summary}（信号 #{sid}）" if sid else str(summary)


def _act_noop(root, args, ctx):
    return True, _pb_render(str(args.get("text") or "（打点，无副作用）"), ctx)


PB_ACTIONS = {
    "ops_check": _act_ops_check, "backup": _act_backup, "export": _act_export,
    "log_work": _act_log_work, "memory": _act_memory, "signal": _act_signal,
    "create_project": _act_create_project, "view_follow": _act_view_follow,
    "checkpoint": _act_checkpoint, "sentry": _act_sentry, "noop": _act_noop,
}


# ---------------------------------------------------------------- 模板入库 / 查询
# 按 root 记，不是进程级布尔：进程内切换根目录（测试多库、MCP 换根）时，
# 进程级 flag 会让第二个库永远不种模板（2026-10-13 回归实测抓到）。
_PB_SEEDED: set = set()


def _pb_insert_steps(conn, pb: dict) -> None:
    for i, s in enumerate(pb["steps"]):
        conn.execute("INSERT INTO playbook_steps(playbook,seq,key,title,action,args,note) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (pb["name"], i + 1, s.get("key") or f"s{i + 1}",
                      s.get("title") or "", s.get("action") or "manual",
                      json.dumps(s.get("args") or {}, ensure_ascii=False),
                      s.get("note") or ""))


def _pb_seed(root) -> int:
    """内置模板入库（builtin=1）。缺失补种；已存在则按**步骤指纹**判断是否新版——
    指纹变了就重建步骤（内置模板用户改不了：playbook_upsert 拒绝覆盖 builtin=1，
    所以库里的内置步骤只可能是旧版出厂状态，重建安全）。

    为什么必须有升级：v2.40 首发时 release 模板漏了回归测试，而「只补缺失」的旧逻辑
    会让这个修正永远推不进已经建过库的真实大脑——模板一发即固化，是活的系统不该有的。
    """
    if root in _PB_SEEDED:
        return 0
    n = 0
    try:
        with db_conn(root) as conn:
            for pb in _BUILTIN_PLAYBOOKS:
                row = conn.execute("SELECT 1 FROM playbooks WHERE name=?", (pb["name"],)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO playbooks(name,title,desc,builtin,created,updated) "
                                 "VALUES(?,?,?,1,?,?)",
                                 (pb["name"], pb["title"], pb["desc"], _now(), _now()))
                    _pb_insert_steps(conn, pb)
                    n += 1
                    continue
                cur = conn.execute("SELECT seq,key,action FROM playbook_steps WHERE playbook=? "
                                   "ORDER BY seq", (pb["name"],)).fetchall()
                old_fp = "|".join(f"{r['seq']}:{r['key']}:{r['action']}" for r in cur)
                new_fp = "|".join(
                    f"{i + 1}:{s.get('key') or f's{i + 1}'}:{s.get('action') or 'manual'}"
                    for i, s in enumerate(pb["steps"]))
                if old_fp == new_fp:
                    continue
                conn.execute("DELETE FROM playbook_steps WHERE playbook=?", (pb["name"],))
                _pb_insert_steps(conn, pb)
                n += 1
    except sqlite3.Error:
        return 0
    _PB_SEEDED.add(root)
    return n


def _pb_step_view(row) -> dict:
    d = dict(row)
    try:
        d["args"] = json.loads(d.get("args") or "{}")
    except (ValueError, TypeError):
        d["args"] = {}
    return d


def playbook_list(root: str) -> list:
    """流程清单（内置在前）：含步骤数、成功跑过几次、最近一次状态。"""
    _pb_seed(root)
    try:
        with db_conn(root) as conn:
            rows = conn.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM playbook_steps s WHERE s.playbook=p.name) AS n_steps,"
                " (SELECT COUNT(*) FROM playbook_runs r WHERE r.playbook=p.name AND r.status='done') AS n_done,"
                " (SELECT r.status FROM playbook_runs r WHERE r.playbook=p.name ORDER BY r.id DESC LIMIT 1) AS last_status"
                " FROM playbooks p ORDER BY p.builtin DESC, p.name").fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def playbook_get(root: str, name: str):
    """单流程定义（含步骤）。不存在返回 None。"""
    _pb_seed(root)
    try:
        with db_conn(root) as conn:
            pb = conn.execute("SELECT * FROM playbooks WHERE name=?", (name,)).fetchone()
            if not pb:
                return None
            steps = conn.execute("SELECT * FROM playbook_steps WHERE playbook=? ORDER BY seq",
                                 (name,)).fetchall()
    except sqlite3.Error:
        return None
    return {"meta": dict(pb), "steps": [_pb_step_view(s) for s in steps]}


def playbook_upsert(root: str, name: str, title: str = "", desc: str = "",
                    steps: list = None, agent: str = "") -> tuple:
    """新建/覆盖**自定义**流程。返回 (错误或 "", 步骤数)。

    内置模板（builtin=1）不可覆盖——要改就换个名字另存，否则下次升级模板会和你的改动打架。
    步骤 action 必须在 PB_ACTIONS 或 manual，写错当场拒（跑的时候才发现=白跑一轮）。
    """
    _pb_seed(root)
    name = (name or "").strip()
    if not re.match(r"^[A-Za-z0-9_\-]{1,40}$", name or ""):
        return "流程名非法：只允许字母/数字/下划线/短横线，1-40 字符", 0
    with db_conn(root) as conn:
        row = conn.execute("SELECT builtin FROM playbooks WHERE name=?", (name,)).fetchone()
        if row and int(row["builtin"] or 0) == 1:
            return f"「{name}」是内置模板，不可覆盖（换个名字另存，如 {name}_mine）", 0
    steps = steps or []
    if not steps:
        return "steps 不能为空（至少一步）", 0
    norm = []
    for i, s in enumerate(steps[:30]):
        if not isinstance(s, dict):
            return f"第 {i + 1} 步不是对象", 0
        act = str(s.get("action") or "manual").strip() or "manual"
        if act != "manual" and act not in PB_ACTIONS:
            return f"第 {i + 1} 步动作「{act}」不在注册表内（可用：{'、'.join(sorted(PB_ACTIONS))}、manual）", 0
        norm.append((i + 1, str(s.get("key") or f"s{i + 1}"), str(s.get("title") or act)[:120], act,
                     json.dumps(s.get("args") or {}, ensure_ascii=False),
                     str(s.get("note") or "")[:400]))
    with db_conn(root) as conn:
        conn.execute("INSERT INTO playbooks(name,title,desc,builtin,created,updated) VALUES(?,?,?,0,?,?) "
                     "ON CONFLICT(name) DO UPDATE SET title=excluded.title, desc=excluded.desc, updated=excluded.updated",
                     (name, title or name, desc or "", _now(), _now()))
        conn.execute("DELETE FROM playbook_steps WHERE playbook=?", (name,))
        conn.executemany("INSERT INTO playbook_steps(playbook,seq,key,title,action,args,note) "
                         "VALUES(?,?,?,?,?,?,?)", [(name, *s) for s in norm])
    return "", len(norm)


# ---------------------------------------------------------------- 执行器
def _pb_step_finish(root, run_id, seq, status, output) -> None:
    with db_conn(root) as conn:
        conn.execute("UPDATE playbook_run_steps SET status=?, output=?, finished=? "
                     "WHERE run_id=? AND seq=?",
                     (status, str(output or "")[:600], _now(), run_id, seq))


def _pb_execute(root: str, run_id: int, steps: list, ctx: dict) -> dict:
    """按 seq 逐步执行：已完成跳过、manual 停下等人、其余真跑。

    失败即停（step.on_fail=continue 除外）——不停的话后面步骤建立在坏基础上，
    报错会离根因越来越远。停在哪一步就是断点，修完 resume 从那一步继续。
    """
    try:
        with db_conn(root) as conn:
            rs = [dict(r) for r in conn.execute(
                "SELECT * FROM playbook_run_steps WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()]
    except sqlite3.Error as e:
        return {"status": "failed", "steps": [], "error": f"读执行状态失败：{e}"}
    by_seq = {int(s["seq"]): s for s in steps}
    status, outs = "done", []
    for r in rs:
        seq = int(r["seq"])
        if r["status"] == "done":
            ctx["out"][r["key"]] = r.get("output") or ""
            outs.append({"seq": seq, "key": r["key"], "title": r["title"],
                         "status": "done", "output": r.get("output") or ""})
            continue
        step = by_seq.get(seq, {})
        action = str(step.get("action") or "manual").strip() or "manual"
        title = step.get("title") or action
        if action == "manual":
            note = step.get("note") or "需人工完成（做完后 hub_playbook action=resume 继续）"
            _pb_step_finish(root, run_id, seq, "manual", note)
            outs.append({"seq": seq, "key": r["key"], "title": title, "status": "manual", "output": note})
            status = "waiting"
            break
        fn = PB_ACTIONS.get(action)
        if fn is None:
            _pb_step_finish(root, run_id, seq, "failed", f"未知动作：{action}")
            outs.append({"seq": seq, "key": r["key"], "title": title, "status": "failed",
                         "output": f"未知动作：{action}"})
            status = "failed"
            break
        try:
            ok, out = fn(root, _pb_args(step, ctx), ctx)
        except Exception as _e:  # noqa: BLE001  动作内部异常=该步失败，不炸整个 run
            ok, out = False, f"{type(_e).__name__}: {_e}"
        out = str(out)[:600]
        _pb_step_finish(root, run_id, seq, "done" if ok else "failed", out)
        ctx["out"][r["key"]] = out
        outs.append({"seq": seq, "key": r["key"], "title": title,
                     "status": "done" if ok else "failed", "output": out})
        if not ok and str(step.get("on_fail") or "stop") != "continue":
            status = "failed"
            break
    # 未跑到的步骤也要出现在清单里，否则「1/2 步完成」这种摘要会让人误以为流程只有 2 步
    seen = {o["seq"] for o in outs}
    for r in rs:
        if int(r["seq"]) not in seen:
            outs.append({"seq": int(r["seq"]), "key": r["key"], "title": r["title"],
                         "status": "pending", "output": ""})
    outs.sort(key=lambda o: o["seq"])
    return {"status": status, "steps": outs}


def _pb_run_ctx(name: str, project: str, agent: str, target: str) -> dict:
    return {"project": project or "", "agent": agent or "", "today": _now()[:10],
            "name": name, "target": target or _default_src_dir(), "out": {}}


def _pb_close_run(root, run_id, status, steps) -> str:
    done = sum(1 for s in steps if s["status"] == "done")
    total = len(steps)
    summary = f"{done}/{total} 步完成"
    if status == "waiting":
        summary += "（停在需人工的步骤）"
    elif status == "failed":
        summary += "（有步骤失败，已停在第 " + str(
            next((s["seq"] for s in steps if s["status"] == "failed"), total)) + " 步）"
    with db_conn(root) as conn:
        conn.execute("UPDATE playbook_runs SET status=?, finished=?, cursor=?, summary=? WHERE id=?",
                     (status, _now(),
                      next((s["seq"] for s in steps if s["status"] != "done"), 0), summary, run_id))
    return summary


def playbook_run(root: str, name: str, project: str = "", agent: str = "", target: str = "",
                 dry_run: bool = False) -> dict:
    """跑一次流程。dry_run=True 只列计划不执行（不落 run）。
    返回 {run_id, playbook, status, summary, steps:[{seq,title,status,output}]}。"""
    _pb_seed(root)
    pb = playbook_get(root, name)
    if not pb:
        return {"error": f"流程「{name}」不存在（hub_playbook action=list 看清单）"}
    steps = pb["steps"]
    if not steps:
        return {"error": f"流程「{name}」没有步骤"}
    if dry_run:
        return {"dry_run": True, "playbook": name, "title": pb["meta"]["title"],
                "steps": [{"seq": s["seq"], "title": s["title"], "action": s["action"],
                           "args": _pb_args(s, _pb_run_ctx(name, project, agent, target))}
                          for s in steps]}
    ctx = _pb_run_ctx(name, project, agent, target)
    with db_conn(root) as conn:
        cur = conn.execute("INSERT INTO playbook_runs(playbook,project,agent,status,started,cursor,summary)"
                           " VALUES(?,?,?,'running',?,0,'')", (name, project or "", agent or "", _now()))
        run_id = cur.lastrowid
        conn.executemany("INSERT INTO playbook_run_steps(run_id,seq,key,title,action,status) "
                         "VALUES(?,?,?,?,?,'pending')",
                         [(run_id, s["seq"], s.get("key") or "", s.get("title") or "",
                           s.get("action") or "manual") for s in steps])
    res = _pb_execute(root, run_id, steps, ctx)
    summary = _pb_close_run(root, run_id, res["status"], res["steps"])
    return {"run_id": run_id, "playbook": name, "title": pb["meta"]["title"],
            "status": res["status"], "summary": summary, "steps": res["steps"]}


def playbook_resume(root: str, run_id, agent: str = "", target: str = "") -> dict:
    """断点续跑：manual 步骤视为人已做完（标 done），从第一个未完成步骤继续。
    全流程已完成/已中止的 run 不会被重跑。"""
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        return {"error": f"run_id 非法：{run_id!r}"}
    with db_conn(root) as conn:
        row = conn.execute("SELECT * FROM playbook_runs WHERE id=?", (rid,)).fetchone()
        if not row:
            return {"error": f"run #{rid} 不存在（hub_playbook action=runs 看历史）"}
        run = dict(row)
        if run["status"] in ("done", "aborted"):
            return {"error": f"run #{rid} 已是 {run['status']}，无需续跑（要重跑请新开一次 run）"}
        conn.execute("UPDATE playbook_run_steps SET status='done', finished=?, "
                     "output=COALESCE(NULLIF(output,''), '（人工确认完成）') "
                     "WHERE run_id=? AND status='manual'", (_now(), rid))
        conn.execute("UPDATE playbook_runs SET status='running' WHERE id=?", (rid,))
    pb = playbook_get(root, run["playbook"])
    if not pb:
        return {"error": f"流程「{run['playbook']}」已不存在，无法续跑"}
    ctx = _pb_run_ctx(run["playbook"], run["project"], agent or run["agent"], target)
    res = _pb_execute(root, rid, pb["steps"], ctx)
    summary = _pb_close_run(root, rid, res["status"], res["steps"])
    return {"run_id": rid, "playbook": run["playbook"], "status": res["status"],
            "summary": summary, "steps": res["steps"]}


def playbook_runs(root: str, playbook: str = "", limit: int = 10) -> list:
    """执行历史（新在前）。"""
    sql = "SELECT * FROM playbook_runs"
    args: list = []
    if playbook:
        sql += " WHERE playbook=?"
        args.append(playbook)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 10), 100)))
    try:
        with db_conn(root) as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []


def playbook_run_steps(root: str, run_id) -> list:
    """某次执行的分步结果（顺序）。"""
    try:
        with db_conn(root) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM playbook_run_steps WHERE run_id=? ORDER BY seq", (int(run_id),)).fetchall()]
    except (sqlite3.Error, TypeError, ValueError):
        return []


def playbook_abort(root: str, run_id) -> str:
    """中止一次执行：未完成步骤标 skipped，run 标 aborted（不删——跑过的事实要留着复盘）。"""
    try:
        rid = int(run_id)
    except (TypeError, ValueError):
        return f"run_id 非法：{run_id!r}"
    with db_conn(root) as conn:
        row = conn.execute("SELECT status FROM playbook_runs WHERE id=?", (rid,)).fetchone()
        if not row:
            return f"run #{rid} 不存在"
        if row["status"] in ("done", "aborted"):
            return f"run #{rid} 已是 {row['status']}，无需中止"
        conn.execute("UPDATE playbook_run_steps SET status='skipped', finished=? "
                     "WHERE run_id=? AND status IN ('pending','failed','manual')", (_now(), rid))
        conn.execute("UPDATE playbook_runs SET status='aborted', finished=?, summary='已中止' WHERE id=?",
                     (_now(), rid))
    return f"run #{rid} 已中止（未完成步骤标 skipped，可 hub_playbook action=runs 复盘）"


# ==================================================================== Y2 自动化层（触发器）
# Y1 解决了「流程怎么跑」，Y2 解决「什么时候自动跑」：把「每次心跳顺手检查一遍」
# 「有人发信号就通知我」这类**本该自动的事**交给大脑，不再依赖每个 agent 自觉。
#
# 三条防线，缺一个就是事故：
#   ① enabled 默认 0——触发器是主动行为，配错即刷屏/误动作，必须人显式开；
#   ② 冷却（默认 12h）——心跳一天好几次，不冷却同一件事会反复推；
#   ③ 时间预算——playbook 可能耗时（回归几分钟），心跳里跑必须有上限，超时丢弃不阻塞。

# due=「时点到了」类事件（决策到回顾日 / 任务超期）——与 heartbeat 的区别：
# heartbeat 每次心跳都评估，due 只在**真有到期项**时才跑（没事就不出声）。
TRIGGER_EVENTS = ("heartbeat", "signal", "record", "due")
TRIGGER_ACTIONS = ("notify", "signal", "playbook")
_TRIGGER_DEFAULT_COOLDOWN = 720  # 分钟（12h）


def _jload(s) -> dict:
    """JSON 字段容错读取：脏数据/空串一律当空 dict，不让一条坏配置炸掉整轮评估。"""
    if isinstance(s, dict):
        return s
    try:
        return json.loads(s) if (s or "").strip() else {}
    except (ValueError, TypeError):
        return {}


def _trig_match(cond: dict, ctx: dict) -> bool:
    """条件匹配：等值比对；keyword 走包含（大小写不敏感）。无条件=全匹配。"""
    if not cond:
        return True
    for k, v in cond.items():
        if v in ("", None):
            continue
        got = str(ctx.get(k) or "")
        if k == "keyword":
            if str(v).lower() not in got.lower():
                return False
        elif str(v) != got:
            return False
    return True


def _trig_cooled(last_fired: str, cooldown_min: int) -> bool:
    if not last_fired or not cooldown_min:
        return False
    try:
        dt = datetime.datetime.now() - datetime.datetime.fromisoformat(last_fired)
    except (TypeError, ValueError):
        return False
    return dt.total_seconds() < cooldown_min * 60


def _run_budget(fn, budget: float):
    """带时间预算执行：超时就丢弃结果（不阻塞调用方，典型场景=心跳里跑 playbook）。

    为什么用线程而不是 signal.alarm：Windows 没有 alarm；且超时后无法真杀线程，
    所以只保证「调用方不被拖住」，后台线程仍在跑完（daemon，进程退出即收）。
    """
    import threading  # noqa: PLC0415  只有触发器路径需要
    box: dict = {}

    def _w():
        try:
            box["r"] = fn()
        except Exception as _e:  # noqa: BLE001
            box["e"] = _e

    th = threading.Thread(target=_w, daemon=True)
    th.start()
    th.join(budget)
    if "r" in box:
        return True, box["r"]
    if "e" in box:
        return False, f"{type(box['e']).__name__}: {box['e']}"
    return False, f"超过时间预算 {budget:.0f}s（动作仍在后台跑完，本次结果不计入）"


def due_summary(root: str) -> dict:
    """到期事项汇总（触发器 due 事件的数据源）：决策到回顾日 + 任务超期未完成。

    只读、fail-open（表异常 = 没有到期项）。count=0 时 due 类触发器一律不跑——
    「时点到了才提醒」的触发器，没到期还推就变成了噪音。
    """
    out: dict = {"count": 0, "kinds": [], "detail": ""}
    try:
        dec = decision_review_due(root, limit=20)
    except Exception:  # noqa: BLE001
        dec = []
    over = []
    try:
        with db_conn(root) as conn:
            over = [dict(r) for r in conn.execute(
                "SELECT id,title,project,due FROM tasks WHERE due!='' AND due<date('now') "
                "AND status NOT IN ('done') ORDER BY due ASC LIMIT 20").fetchall()]
    except sqlite3.Error:
        over = []
    out["count"] = len(dec) + len(over)
    if dec:
        out["kinds"].append(f"决策到期 {len(dec)}")
    if over:
        out["kinds"].append(f"任务超期 {len(over)}")
    out["detail"] = "；".join(
        [f"决策#{d.get('record_id')}（{d.get('review_date')} 到期）" for d in dec[:3]]
        + [f"任务#{t['id']}（{t['due']} 到期）" for t in over[:3]])
    return out


def trigger_add(root: str, name: str, on_event: str = "heartbeat", action_type: str = "notify",
                action_args: dict = None, condition: dict = None, cooldown_min: int = None,
                agent: str = "") -> tuple:
    """建触发器。返回 (错误或 "", id)。默认 enabled=0——建完要显式开。"""
    name = (name or "").strip()
    if not name:
        return "name 必填（给自己看的，写清触发什么）", 0
    if on_event not in TRIGGER_EVENTS:
        return f"on_event 必须是 {'、'.join(TRIGGER_EVENTS)}", 0
    if action_type not in TRIGGER_ACTIONS:
        return f"action_type 必须是 {'、'.join(TRIGGER_ACTIONS)}", 0
    args = action_args or {}
    if not isinstance(args, dict):
        return "action_args 必须是对象", 0
    if action_type == "playbook" and not str(args.get("name") or "").strip():
        return "playbook 动作必须在 action_args 里给 name（要跑哪个流程）", 0
    if action_type == "signal" and not str(args.get("content") or "").strip():
        return "signal 动作必须在 action_args 里给 content", 0
    try:
        cd = _TRIGGER_DEFAULT_COOLDOWN if cooldown_min is None else int(cooldown_min)
    except (TypeError, ValueError):
        return "cooldown_min 必须是整数（分钟）", 0
    cd = max(0, min(cd, 10080))  # 上限一周：更长不如改成手动
    with db_conn(root) as conn:
        cur = conn.execute(
            "INSERT INTO triggers(name,on_event,condition,action_type,action_args,enabled,"
            "cooldown_min,last_fired,fire_count,agent,created) VALUES(?,?,?,?,?,0,?,'',0,?,?)",
            (name[:80], on_event, json.dumps(condition or {}, ensure_ascii=False), action_type,
             json.dumps(args, ensure_ascii=False), cd, (agent or "").strip(), _now()))
        return "", cur.lastrowid


def trigger_list(root: str, on_event: str = "") -> list:
    sql = "SELECT * FROM triggers"
    args: list = []
    if on_event:
        sql += " WHERE on_event=?"
        args.append(on_event)
    sql += " ORDER BY enabled DESC, id"
    try:
        with db_conn(root) as conn:
            rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []
    for r in rows:
        r["condition"] = _jload(r.get("condition"))
        r["action_args"] = _jload(r.get("action_args"))
    return rows


def trigger_set_enabled(root: str, tid, enabled: bool) -> str:
    try:
        i = int(tid)
    except (TypeError, ValueError):
        return f"触发器 id 非法：{tid!r}"
    with db_conn(root) as conn:
        if not conn.execute("SELECT 1 FROM triggers WHERE id=?", (i,)).fetchone():
            return f"触发器 #{i} 不存在"
        conn.execute("UPDATE triggers SET enabled=? WHERE id=?", (1 if enabled else 0, i))
    return f"触发器 #{i} 已{'启用' if enabled else '停用'}"


def trigger_del(root: str, tid) -> str:
    try:
        i = int(tid)
    except (TypeError, ValueError):
        return f"触发器 id 非法：{tid!r}"
    with db_conn(root) as conn:
        cur = conn.execute("DELETE FROM triggers WHERE id=?", (i,))
    return f"触发器 #{i} 已删除" if cur.rowcount else f"触发器 #{i} 不存在"


def _trig_stamp(root: str, tid: int) -> None:
    """记录触发时刻并计数（冷却判断的事实依据）。"""
    with db_conn(root) as conn:
        conn.execute("UPDATE triggers SET last_fired=?, fire_count=fire_count+1 WHERE id=?",
                     (_now(), tid))


def _trig_act(root: str, t: dict, ctx: dict, budget: float):
    """执行一次触发动作。返回 (ok, text)。"""
    atype = t.get("action_type") or "notify"
    raw = t.get("action_args") or {}
    args = {k: (_pb_render(v, ctx) if isinstance(v, str) else v) for k, v in raw.items()}
    if atype == "notify":
        return True, args.get("text") or t.get("name") or "（触发）"
    if atype == "signal":
        ok, out = _run_budget(
            lambda: signal_send(root, args.get("from_agent") or t.get("agent") or "system",
                                args.get("content") or "", args.get("to_agent") or "",
                                args.get("kind") or "msg",
                                args.get("project") or ctx.get("project") or ""), budget)
        if not ok:
            return False, out
        tip, sid = out
        return (sid > 0), str(tip)
    if atype == "playbook":
        nm = str(args.get("name") or "").strip()
        if not nm:
            return False, "playbook 动作缺 name"
        ok, res = _run_budget(
            lambda: playbook_run(root, nm, args.get("project") or ctx.get("project") or "",
                                 args.get("agent") or t.get("agent") or "",
                                 args.get("target") or ""), budget)
        if not ok:
            return False, res
        if res.get("error"):
            return False, str(res["error"])
        return True, f"playbook {nm} → {res['status']}（{res['summary']}）"
    return False, f"未知动作类型：{atype}"


def trigger_eval(root: str, event: str, ctx: dict = None, budget_sec: float = 20) -> list:
    """评估某事件下的触发器并执行（内部调用点：心跳/发信号/写记录）。

    只跑 enabled=1 且条件匹配且不在冷却内的。返回 [{id,name,status,text}]，
    status: fired / failed / cooled。任何一项炸都不影响其他项、不影响调用方。
    """
    ctx = dict(ctx or {})
    ctx.setdefault("event", event)
    if event not in TRIGGER_EVENTS:
        return []
    if event == "due":
        # 先算「有没有到期的事」：没有就不往下走（到期类触发器没到期项还推 = 噪音）
        pending = due_summary(root)
        if not pending["count"]:
            return []
        ctx["due_count"] = str(pending["count"])
        ctx["due_kinds"] = "、".join(pending["kinds"])
        ctx["due_detail"] = pending["detail"]
    try:
        with db_conn(root) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM triggers WHERE on_event=? AND enabled=1 ORDER BY id",
                (event,)).fetchall()]
    except sqlite3.Error:
        return []
    out = []
    for t in rows:
        t["condition"] = _jload(t.get("condition"))
        t["action_args"] = _jload(t.get("action_args"))
        if not _trig_match(t["condition"], ctx):
            continue
        if _trig_cooled(t.get("last_fired"), int(t.get("cooldown_min") or 0)):
            out.append({"id": t["id"], "name": t["name"], "status": "cooled", "text": ""})
            continue
        try:
            ok, text = _trig_act(root, t, ctx, budget_sec)
        except Exception as _e:  # noqa: BLE001  单个触发器炸了不能拖垮心跳
            ok, text = False, f"{type(_e).__name__}: {_e}"
        _trig_stamp(root, t["id"])
        out.append({"id": t["id"], "name": t["name"], "status": "fired" if ok else "failed",
                    "text": str(text)[:400]})
    return out


def trigger_fire(root: str, tid, ctx: dict = None, force: bool = False) -> dict:
    """手动触发一次（测试/立即执行）。force=True 忽略冷却与 enabled（调试用）。"""
    try:
        i = int(tid)
    except (TypeError, ValueError):
        return {"error": f"触发器 id 非法：{tid!r}"}
    with db_conn(root) as conn:
        row = conn.execute("SELECT * FROM triggers WHERE id=?", (i,)).fetchone()
    if not row:
        return {"error": f"触发器 #{i} 不存在"}
    t = dict(row)
    if not force:
        if not int(t.get("enabled") or 0):
            return {"error": f"触发器 #{i} 未启用（先 action=enable，或 force=true 强制试跑）"}
        if _trig_cooled(t.get("last_fired"), int(t.get("cooldown_min") or 0)):
            return {"error": f"触发器 #{i} 在冷却中（上次 {t.get('last_fired', '')[:16]}，"
                             f"冷却 {t.get('cooldown_min')} 分钟）"}
    t["condition"] = _jload(t.get("condition"))
    t["action_args"] = _jload(t.get("action_args"))
    ctx = dict(ctx or {})
    ctx.setdefault("event", t.get("on_event") or "manual")
    try:
        ok, text = _trig_act(root, t, ctx, 120)
    except Exception as _e:  # noqa: BLE001
        ok, text = False, f"{type(_e).__name__}: {_e}"
    _trig_stamp(root, i)
    return {"id": i, "name": t.get("name"), "status": "fired" if ok else "failed",
            "text": str(text)[:600]}


# ==================================================================== Y3 知识图谱（关联检索）
# 检索解决「能不能找到」，图谱解决「找到了但不知道它跟什么有关」。
# 边分四类：mention（[[双链]]，写入侧自动）/ link（手工）/ tag（记忆→标签）/ in（记录→项目）。
# 后两类由 graph_build 物化（幂等重建，随时可重跑）；前两类写入侧维护，重跑不动。
# 只做查询与统计，**不自动改数据**——图谱的用途是让人看见关联，不是替人重构知识。

GRAPH_NODE_KINDS = ("record", "memory", "note", "project", "tag")


def _node(ref: str) -> tuple:
    """节点串 → (kind, id)。支持 record:12 / memory:3 / tag:踩坑 / project:项目名。"""
    ref = (ref or "").strip()
    if ":" in ref:
        k, v = ref.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k in GRAPH_NODE_KINDS and v:
            return k, v
    return "", ""


def graph_build(root: str) -> dict:
    """物化结构边（幂等：先删同类旧边再插）。返回统计。

    tag 边让「同标签的记忆」在图上相连，in 边让「同项目的记录」相连——
    这两类边不是人写的，是数据本身的结构，不物化就永远看不到。
    """
    stat = {"tag": 0, "in": 0, "memories": 0, "records": 0}
    now = _now()
    with db_conn(root) as conn:
        conn.execute("DELETE FROM relations WHERE kind IN ('tag','in')")
        try:
            mems = conn.execute("SELECT id, tags, project FROM memories WHERE " + _live_sql()).fetchall()
        except sqlite3.Error:
            mems = []
        for r in mems:
            stat["memories"] += 1
            for t in re.split(r"[,，]", r["tags"] or ""):
                t = t.strip()
                if not t:
                    continue
                conn.execute("INSERT OR IGNORE INTO relations"
                             "(from_kind,from_id,to_kind,to_ref,kind,created) VALUES('memory',?,'tag',?,'tag',?)",
                             (int(r["id"]), t, now))
                stat["tag"] += 1
            # 记忆归属项目也成边：否则「同项目的记录与记忆」在图上永不相交（实测路径查询全断）
            if (r["project"] or "").strip():
                conn.execute("INSERT OR IGNORE INTO relations"
                             "(from_kind,from_id,to_kind,to_ref,kind,created) VALUES('memory',?,'project',?,'in',?)",
                             (int(r["id"]), r["project"].strip(), now))
                stat["in"] += 1
        try:
            recs = conn.execute("SELECT id, project FROM records WHERE " + _rec_live_sql()).fetchall()
        except sqlite3.Error:
            recs = []
        for r in recs:
            p = (r["project"] or "").strip()
            if not p:
                continue
            stat["records"] += 1
            conn.execute("INSERT OR IGNORE INTO relations"
                         "(from_kind,from_id,to_kind,to_ref,kind,created) VALUES('record',?,'project',?,'in',?)",
                         (int(r["id"]), p, now))
            stat["in"] += 1
    return stat


def _graph_adj(conn) -> dict:
    """全表邻接表（无向）：(kind,ref) -> [(邻居, 边类型)]。库规模下全量加载最省心。"""
    try:
        rows = conn.execute("SELECT from_kind,from_id,to_kind,to_ref,kind FROM relations").fetchall()
    except sqlite3.Error:
        return {}
    adj: dict = {}
    for e in rows:
        a = (e["from_kind"], str(e["from_id"]))
        b = (e["to_kind"], str(e["to_ref"]))
        adj.setdefault(a, []).append((b, e["kind"]))
        adj.setdefault(b, []).append((a, e["kind"]))
    return adj


def graph_stats(root: str, build: bool = True) -> dict:
    """图谱体检：边数/边型分布/节点数/已连条目/孤岛数/最大簇。"""
    if build:
        graph_build(root)
    with db_conn(root) as conn:
        try:
            edges = conn.execute("SELECT COUNT(*) c FROM relations").fetchone()["c"]
            by_kind = {r["kind"]: r["c"] for r in
                       conn.execute("SELECT kind, COUNT(*) c FROM relations GROUP BY kind")}
            n_mem = conn.execute("SELECT COUNT(*) c FROM memories WHERE " + _live_sql()).fetchone()["c"]
            n_rec = conn.execute("SELECT COUNT(*) c FROM records WHERE " + _rec_live_sql()).fetchone()["c"]
            # linked 与 graph_isolated 同口径（2026-10-13 修订）：记忆看**标签边**、记录看任何边。
            # 旧口径（记忆有任何边即算连上）会把"有项目无标签"的记忆全算成已连，
            # 而它们恰恰是最该补标签的一批，stats 会给出虚假的安心感。
            lm = conn.execute("SELECT COUNT(DISTINCT from_id) c FROM relations "
                              "WHERE from_kind='memory' AND kind='tag'").fetchone()["c"]
            lr = conn.execute("SELECT COUNT(DISTINCT from_id) c FROM relations "
                              "WHERE from_kind='record'").fetchone()["c"]
            linked = int(lm or 0) + int(lr or 0)
        except sqlite3.Error:
            return {"error": "图谱统计失败（表缺失？）"}
    cl = graph_clusters(root, limit=1, build=False)
    return {"edges": edges, "by_kind": by_kind, "nodes": {"record": n_rec, "memory": n_mem},
            "linked": linked, "isolated": max(0, n_rec + n_mem - linked),
            "largest_cluster": (cl[0]["size"] if cl else 0)}


def graph_neighbors(root: str, ref: str, limit: int = 30) -> dict:
    """某节点的直接邻居（出边/入边分开列，带来源摘要）。"""
    kind, sid = _node(ref)
    if not kind:
        return {"error": f"节点格式不对：{ref!r}（应如 record:12 / memory:3 / tag:踩坑 / project:项目名）"}
    lim = max(1, min(int(limit or 30), 100))
    try:
        iid = int(sid)
    except (TypeError, ValueError):
        iid = -1
    with db_conn(root) as conn:
        out_e = [dict(r) for r in conn.execute(
            "SELECT * FROM relations WHERE from_kind=? AND from_id=? ORDER BY id DESC LIMIT ?",
            (kind, iid, lim)).fetchall()]
        in_e = [dict(r) for r in conn.execute(
            "SELECT * FROM relations WHERE to_kind=? AND to_ref=? ORDER BY id DESC LIMIT ?",
            (kind, sid, lim)).fetchall()]
        for r in out_e + in_e:
            r["snippet"], r["project"] = _src_snippet(conn, r["from_kind"], int(r["from_id"] or 0))
    return {"node": f"{kind}:{sid}", "out": out_e, "in": in_e}


def graph_related(root: str, ref: str, limit: int = 10) -> list:
    """与某条目最相关的条目（二跳内，按 距离衰减 + 显式边加权 排序）。

    与检索的分工：检索按「词像不像」，这里按「图上近不近」——
    一条没提到关键词但同项目同标签的记录，检索找不到，图谱能。
    """
    kind, sid = _node(ref)
    if not kind:
        return []
    lim = max(1, min(int(limit or 10), 50))
    start = (kind, sid)
    with db_conn(root) as conn:
        adj = _graph_adj(conn)
        scores: dict = {}
        seen = {start}
        frontier = [start]
        for depth in (1, 2):
            nxt = []
            for n in frontier:
                for nb, ek in adj.get(n, []):
                    if nb in seen:
                        continue
                    seen.add(nb)
                    nxt.append(nb)
                    if nb[0] in ("record", "memory"):
                        # 显式边（双链/手工）比结构边（同标签/同项目）更能说明「有关」
                        scores[nb] = scores.get(nb, 0.0) + (2.0 / depth) + (0.5 if ek in ("mention", "link") else 0)
            frontier = nxt
        out = sorted(scores.items(), key=lambda kv: -kv[1])[:lim]
        res = []
        for (k, i), s in out:
            try:
                ii = int(i)
            except (TypeError, ValueError):
                continue
            snip, proj = _src_snippet(conn, k, ii, 60)
            res.append({"node": f"{k}:{i}", "score": round(s, 2), "snippet": snip, "project": proj})
    return res


def graph_path(root: str, a: str, b: str, max_depth: int = 4) -> dict:
    """两节点间最短路径（BFS）。找不到就明说找不到——编一条路径比说不知道更糟。"""
    ka, ia = _node(a)
    kb, ib = _node(b)
    if not ka or not kb:
        return {"error": f"节点格式不对：{a!r} 或 {b!r}（应如 record:12 / tag:踩坑）"}
    if (ka, ia) == (kb, ib):
        return {"found": True, "path": [f"{ka}:{ia}"], "depth": 0}
    depth = max(1, min(int(max_depth or 4), 6))
    with db_conn(root) as conn:
        adj = _graph_adj(conn)
    start, goal = (ka, ia), (kb, ib)
    prev: dict = {start: None}
    frontier = [start]
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for nb, _ek in adj.get(n, []):
                if nb in prev:
                    continue
                prev[nb] = n
                if nb == goal:
                    path, cur = [], goal
                    while cur is not None:
                        path.append(f"{cur[0]}:{cur[1]}")
                        cur = prev[cur]
                    return {"found": True, "path": list(reversed(path)), "depth": len(path) - 1}
                nxt.append(nb)
        frontier = nxt
        if not frontier:
            break
    return {"found": False, "depth": depth,
            "hint": f"{depth} 跳内不相连（可先 action=build 重建结构边，或放大 max_depth）"}


def graph_isolated(root: str, limit: int = 20, build: bool = False) -> list:
    """孤岛清单 + 补标签建议：**没有标签边**的记忆 + 没有任何边的记录。

    口径说明（2026-10-13 修订）：旧口径「无任何边」把"有项目但没标签"的记忆全排除了——
    而它们恰恰是最该补标签的一批（项目边太泛，说明不了语义关联）。标签边才是
    「顺带唤起」的主要载体，所以记忆侧只看标签边；记录没有标签概念，仍看任何边。
    stats 的 isolated 计数与本函数同口径，两边数字对得上。

    建议标签：同项目其他条目的高频标签里，这条还没有的——修不修由人定，
    但别再让人从零想「该打什么标签」。
    """
    if build:
        graph_build(root)
    lim = max(1, min(int(limit or 20), 100))
    # 同项目标签频次（建议来源）
    proj_tags: dict = {}
    try:
        with db_conn(root) as conn:
            for r in conn.execute("SELECT project, tags FROM memories WHERE " + _live_sql()):
                if not (r["project"] or "").strip():
                    continue
                bucket = proj_tags.setdefault(r["project"].strip(), {})
                for t in re.split(r"[,，]", r["tags"] or ""):
                    t = t.strip()
                    if t:
                        bucket[t] = bucket.get(t, 0) + 1
    except sqlite3.Error:
        proj_tags = {}
    with db_conn(root) as conn:
        try:
            lr = {str(r["from_id"]) for r in conn.execute(
                "SELECT DISTINCT from_id FROM relations WHERE from_kind='record'").fetchall()}
            # 记忆侧只认**标签边**（含入边：被打标签的记忆）
            lm = {str(r["from_id"]) for r in conn.execute(
                "SELECT DISTINCT from_id FROM relations WHERE from_kind='memory' AND kind='tag'").fetchall()}
            lm |= {str(r["to_ref"]) for r in conn.execute(
                "SELECT DISTINCT to_ref FROM relations WHERE to_kind='tag'").fetchall()}
        except sqlite3.Error:
            return []
        out = []
        try:
            recs = conn.execute("SELECT id,title,content,project,date FROM records WHERE "
                                + _rec_live_sql() + " ORDER BY id DESC LIMIT 3000").fetchall()
        except sqlite3.Error:
            recs = []
        for r in recs:
            if str(r["id"]) in lr:
                continue
            out.append({"node": f"record:{r['id']}", "kind": "record",
                        "snippet": (r["title"] or r["content"] or "")[:60],
                        "project": r["project"] or "", "date": r["date"] or "",
                        "suggested_tags": []})
        try:
            mems = conn.execute("SELECT id,content,project,kind,tags FROM memories WHERE "
                                + _live_sql() + " ORDER BY id DESC LIMIT 3000").fetchall()
        except sqlite3.Error:
            mems = []
        for m in mems:
            if str(m["id"]) in lm:
                continue
            # 建议标签：同项目高频标签里这条还没有的（最多 2 个）
            sug = []
            mine = {t.strip() for t in re.split(r"[,，]", m["tags"] or "") if t.strip()}
            for t, _n in sorted((proj_tags.get((m["project"] or "").strip()) or {}).items(),
                                key=lambda kv: (-kv[1], kv[0])):
                if t not in mine:
                    sug.append(t)
                if len(sug) >= 2:
                    break
            out.append({"node": f"memory:{m['id']}", "kind": "memory",
                        "snippet": (m["content"] or "")[:60],
                        "project": m["project"] or "", "date": "", "suggested_tags": sug})
    return out[:lim]


def graph_clusters(root: str, limit: int = 8, min_size: int = 2, build: bool = False) -> list:
    """知识簇（并查集连通分量，只统计 record/memory 节点）。

    用途：看见「知识孤岛」——孤立条目没跟任何东西相连，检索时最难被顺带唤起，
    也最容易被遗忘。规模最大的簇通常就是当前真正的主线。
    """
    if build:
        graph_build(root)
    lim = max(1, min(int(limit or 8), 30))
    with db_conn(root) as conn:
        try:
            rows = conn.execute("SELECT from_kind,from_id,to_kind,to_ref FROM relations").fetchall()
        except sqlite3.Error:
            return []
        parent: dict = {}

        def find(x):
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        for r in rows:
            a = (r["from_kind"], str(r["from_id"]))
            b = (r["to_kind"], str(r["to_ref"]))
            parent.setdefault(a, a)
            parent.setdefault(b, b)
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
        groups: dict = {}
        for n in list(parent):
            if n[0] not in ("record", "memory"):
                continue
            groups.setdefault(find(n), []).append(n)
        out = [{"size": len(v), "nodes": v} for v in groups.values() if len(v) >= max(2, min_size)]
        out.sort(key=lambda g: -g["size"])
        for g in out[:lim]:
            reps = []
            for k, i in g["nodes"][:3]:
                try:
                    ii = int(i)
                except (TypeError, ValueError):
                    continue
                s, _p = _src_snippet(conn, k, ii, 48)
                reps.append(f"{k}:{i}｜{s}")
            g["sample"] = reps
            g["nodes"] = [f"{k}:{i}" for k, i in g["nodes"]]
    return out[:lim]