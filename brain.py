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
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import core

DB_NAME = "brain.db"
BACKUP_DIR = "brain-backups"
# 在线库快照（backup_brain → _hub/brain-backups/）的轮转上限。
# R255：30 → 10，且与 BACKUP_FAMILIES["snapshot"] **共用这一个常量**。此前是两个数：
# backup_brain 留 30 份、backup_prune 的策略表认为只该留 10 份 —— 于是本机 30 份快照里
# 永远有 20 份被判「可清理」，backup_health 常年报「可清理 1374MB」；真清到 10 份也没用，
# window.py 每次退出自动备份，几轮就长回 30 份（清了又长，永远清不掉＝等于不可清）。
# 10 份 × 每份约 18MB ≈ 180MB，覆盖最近 10 次退出，回滚需求够用。
BACKUP_KEEP = 10
KINDS = ("fact", "preference", "lesson", "project", "note")

# ---------------------------------------------------------------- 新鲜度策略（R4）
# 「Facts Should Expire」模式（外部研究，2026-10）：事实行该带「我的真相多旧」——
# 状态 fresh/stale/expired **在读时计算**（无清扫进程、无定时任务），且**过期不删除**：
# 过期事实仍是证据，只是更弱（「供应商三月在 Austin」胜过「未知」）。
# 分档依据（同研究）：耐久知识（决策/约束/偏好）长窗口，易变运维事实短窗口——
# 「如果每条事实同一个 TTL，等于每隔不久把全库重审一遍」。
# 本库口径：fact=环境事实（路径/端口/版本，随升级就变）最短；note=随手记最 ephemeral；
# preference/lesson=人的认知，最耐久。checked 信号优先用显式确认列（R5 的
# hub_memory_confirm 会写），没有时退回 last_hit（被检索≈最近有人看过它，弱确认）。
FRESH_POLICY = {
    # kind: (fresh_days, expire_days)——age ≤ fresh 为 fresh；fresh < age ≤ expire 为 stale；
    #       age > expire 或 valid_until 过期 → expired。未知 kind 用 note 档（最保守）。
    "fact": (14, 90),          # 环境事实：机器配置两周就该复核，三个月不信
    "preference": (180, 730),  # 用户偏好：人的习惯以季计
    "lesson": (365, 1825),     # 踩坑经验：教训很少变假，五年一审
    "project": (90, 365),      # 项目进展：跟项目周期走
    "note": (7, 30),           # 随手记：本来就该速朽
}
FRESH_STATES = ("fresh", "stale", "expired", "unknown")
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
    with _SENTINEL_LOCK:
        c = _SENTINELS.get(p)
        if c is not None:
            try:
                c.execute("SELECT 1").fetchone()
                return
            except sqlite3.Error:
                _SENTINELS.pop(p, None)
        # R298：exists() 放在哨兵命中之后。原来每条短连接都要 stat 一次库文件
        # （Windows 实测 1.4ms/次，一次 search_all 23 条连接 ≈ 32ms 全花在确认
        # 「库还在」——而有常驻句柄时它必然还在）。哨兵缺席（首连/被淘汰）才值得 stat。
        if not os.path.exists(p):
            return
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


def _begin_write(conn) -> str:
    """显式抢占写锁（BEGIN IMMEDIATE），供「先查后写」的操作跨进程串行。

    WAL 的延迟事务里两个连接可以同时读到「无冲突」，再先后提交就造成双持有——
    文件租约的检查/续租/回收都必须先拿写锁再读，否则检查与写入之间是可乘之隙。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        return ""
    except sqlite3.OperationalError as e:
        return f"数据库繁忙，请稍后重试（{e}）"


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
    superseded_by INTEGER DEFAULT 0,
    task_id INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_records_proj ON records(project);
CREATE INDEX IF NOT EXISTS idx_records_date ON records(date);
-- R769：时间线页切页时同步填过滤器下拉，6 万记录库实测 list_projects 111.5ms
-- （逐项目 MAX(date)/COUNT(*) 相关子查询回表扫全部记录）→ 覆盖索引后 1.7ms；
-- records_agents 的 DISTINCT lower(agent) 28.4ms → 13.9ms。查询语句不改。
CREATE INDEX IF NOT EXISTS idx_records_proj_status_date ON records(project, status, date);
CREATE INDEX IF NOT EXISTS idx_records_status_agent ON records(status, agent);
-- R778：20 万记录库 today/timeline 同步切页 127/146ms（6 万时 60/75ms）。team_digest 的
-- 「近 N 天」窗口把 date 包进 COALESCE(NULLIF(date,''),substr(created,1,10))，普通 date
-- 索引用不上，实际扫全部 active 行（两条合计 91ms）；表达式索引后同一查询 0ms。
-- records_page 首页 ORDER BY date DESC, id DESC 原来靠临时排序 20 万行（64–80ms），
-- (status,date) 让 SQLite 反向扫索引直接取前 50 条（0.2ms）。
CREATE INDEX IF NOT EXISTS idx_records_status_day
    ON records(status, COALESCE(NULLIF(date,''), substr(created,1,10)));
CREATE INDEX IF NOT EXISTS idx_records_status_date ON records(status, date);
-- R780：统计页 20 万库 664ms 的两处大头。①extract_todos 取最近 400 条
-- （WHERE status='active' ORDER BY id DESC）没有 (status,id) 索引，20 万行先排序 63ms；
-- ②体检的裸标题计数 title GLOB '[0-9]…（*）'（字符类开头）只能全表扫，205ms；
-- 部分索引只收命中行（正常库近乎空），查询计划可直接命中它。
CREATE INDEX IF NOT EXISTS idx_records_status_id ON records(status, id);
CREATE INDEX IF NOT EXISTS idx_records_bare_title ON records(status)
    WHERE title GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]（*）';
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
    maturity TEXT DEFAULT "draft",
    confirmed TEXT DEFAULT ""
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
    ts TEXT, agent TEXT, action TEXT, target TEXT, backup TEXT, note TEXT,
    prev_hash TEXT DEFAULT '', hash TEXT DEFAULT ''
);
-- R91 治理查询索引：安全态势/日结按 action+ts 过滤，审计导出按 ts 切片。
-- 只加索引不改数据；init_db 每次幂等补建，存量库同样生效。
CREATE INDEX IF NOT EXISTS idx_journal_action_ts ON journal(action, ts);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(ts);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, agent TEXT, project TEXT, title TEXT, detail TEXT,
    undo TEXT, status TEXT DEFAULT "open",
    repeat_count INTEGER DEFAULT 1, last_ts TEXT DEFAULT '',
    last_detail TEXT DEFAULT ''
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
    records INTEGER DEFAULT 0,
    control_state TEXT DEFAULT "active",
    control_note TEXT DEFAULT "",
    control_at TEXT DEFAULT "",
    skills TEXT DEFAULT "",
    role TEXT DEFAULT "worker"
);
-- R6：接入验证快照（每 agent 一行，最新一次 verify 的结果）。
-- 为什么落库：现场探测只能回答「现在配没配」，回答不了「上次验证过吗、当时哪项没过」。
-- 重启/改配置后，没有快照就没人知道上一轮的「已接入」结论是否还成立。
CREATE TABLE IF NOT EXISTS agent_onboarding (
    agent TEXT PRIMARY KEY,
    ts TEXT DEFAULT "",
    ok INTEGER DEFAULT 0,
    steps TEXT DEFAULT "",
    note TEXT DEFAULT "",
    fingerprint TEXT DEFAULT "",
    base TEXT DEFAULT ""
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
-- C3：只读能力工具的调用留痕。**为什么单独一张表**：op_audit 语义是「写操作审计」，
-- searches 语义是「检索日志」——而 hub_distill/duplicates/list_skills/get_record 这类
-- 只读**能力型**工具两个源都不写，于是反馈报告永远把它们显示成「从未使用」（真缺口：
-- 不是没用，是没有留痕机制）。语义混进 op_audit 会污染审计，故独立成表。
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT "",
    agent TEXT DEFAULT "",
    tool TEXT DEFAULT "",
    channel TEXT DEFAULT ""
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
    deliverables TEXT DEFAULT "",
    skills TEXT DEFAULT "",
    created TEXT DEFAULT "",
    updated TEXT DEFAULT "",
    progress_at TEXT DEFAULT "",
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
-- R11 文件租约（2026-10-21）：任务租约防「撞任务」，文件租约防「撞文件」。
-- 多家 agent 同仓并发改同一文件是并行协作最高频的失败源（共享状态并发写入）；
-- 本表是**咨询式**租约：不拦截写盘，但 acquire/check 报冲突、心跳提示本项目谁在写。
-- path 为规范化绝对路径（Windows 小写、正斜杠），display_path 面向用户（根内相对路径）。
-- fence 按同一 path 单调递增，供「迟到写入」识别；expires_at 过期即可被他人接管。
CREATE TABLE IF NOT EXISTS file_leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    display_path TEXT DEFAULT "",
    project TEXT DEFAULT "",
    owner TEXT DEFAULT "",
    task_id INTEGER DEFAULT 0,
    reason TEXT DEFAULT "",
    ttl_min INTEGER DEFAULT 30,
    fence INTEGER DEFAULT 1,
    acquired_at TEXT DEFAULT "",
    renewed_at TEXT DEFAULT "",
    expires_at TEXT DEFAULT "",
    released_at TEXT DEFAULT "",
    released_by TEXT DEFAULT ""
);
CREATE INDEX IF NOT EXISTS idx_file_leases_path ON file_leases(path, released_at);
CREATE INDEX IF NOT EXISTS idx_file_leases_owner ON file_leases(owner, released_at);
CREATE INDEX IF NOT EXISTS idx_file_leases_expires ON file_leases(expires_at);
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
    closed_by TEXT DEFAULT "",
    closed_at TEXT DEFAULT "",
    close_note TEXT DEFAULT "",
    created TEXT DEFAULT ""
);
CREATE INDEX IF NOT EXISTS idx_handoffs_status ON handoffs(status);
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
-- D1/R2（2026-10-18）写工具幂等键：agent 重试是必然（网络超时/循环重发），
-- 不幂等的写工具让「超时重试」变成「重复记账」——外部研究一致点名这是写工具
-- 第一优先项（CallSphere/Thomas De Vos/datarekha）。语义：同 (agent, key) 同
-- 内容 → 重放首次结果；同键不同内容 → 冲突拒收（digest 防键复用）。
CREATE TABLE IF NOT EXISTS idem_keys (
    agent TEXT NOT NULL,
    key TEXT NOT NULL,
    tool TEXT NOT NULL,
    digest TEXT NOT NULL,
    result TEXT NOT NULL,
    ref INTEGER,
    ts TEXT NOT NULL,
    PRIMARY KEY (agent, key)
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
    # R297：拒收必须给出登记出口——新脑上没有本机主力 agent（如 qoder）时，
    # 只说「可用：…」会让人以为无法挽回；config.agents.json 是零改码登记入口。
    reg_hint = ""
    if root:
        reg_hint = (f"；若是新 agent，把它加进 {Path(root) / core.AGENTS_CONFIG_NAME}"
                    "（{\"agents\":[{\"agent\":\"名字\",\"instr_path\":…}]}，零改码，"
                    "格式见运维手册「配置与数据位置」）")
    return "", (f"agent 名「{raw}」不在注册表内，已拒收（避免时间线归属/统计出错）。"
                f"可用：{avail}{reg_hint}；确实无法归属时传 unknown。")


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


def onboarding_save(root: str, agent: str, ok: bool, steps: list, note: str = "",
                    fingerprint: dict | str = "", base: str = "") -> str:
    """写接入验证快照（每 agent 一行，覆盖上一次）。返回错误或 ""。

    fingerprint 记录验证时加载的代码内容指纹（文件名→md5）：部署 copy2 保留旧 mtime 时，
    只比时间会把「代码已换」判成「仍然一致」。base 记录这份指纹来自哪个代码树（源码目录
    还是部署目录）——同一个 agent 可能被 CLI 从源码树验证、又被 MCP 从部署树复查，跨树
    比对会把「另一个树里多出的文件」误判成代码变更。旧调用不传则存空，读取时沿用旧口径。
    """
    key = _norm_agent(agent)
    if not key:
        return "agent 必填"
    if isinstance(fingerprint, dict):
        fingerprint = json.dumps(fingerprint, sort_keys=True, ensure_ascii=False)
    ensure_schema(root)
    try:
        with db_conn(root) as conn:
            conn.execute(
                "INSERT INTO agent_onboarding(agent,ts,ok,steps,note,fingerprint,base) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(agent) DO UPDATE SET ts=excluded.ts, ok=excluded.ok, "
                "steps=excluded.steps, note=excluded.note, fingerprint=excluded.fingerprint, "
                "base=excluded.base",
                (key, _now(), 1 if ok else 0, json.dumps(steps or [], ensure_ascii=False),
                 note or "", fingerprint or "", (base or "").strip()))
        return ""
    except sqlite3.Error as e:
        return f"接入快照写入失败：{e}"


def onboarding_get(root: str, agent: str = "") -> list:
    """读接入验证快照。agent 为空返回全部；表不存在（旧库未跑 ensure_schema）返回 []。

    fingerprint 解析为 dict；旧快照无指纹返回 {}（调用方据此回落 mtime 判定）。
    """
    sql = "SELECT agent, ts, ok, steps, note, fingerprint, base FROM agent_onboarding"
    args: list = []
    if agent:
        sql += " WHERE agent=?"
        args.append(_norm_agent(agent))
    sql += " ORDER BY ts DESC"
    try:
        with db_conn(root) as conn:
            rows = []
            for r in conn.execute(sql, args):
                d = dict(r)
                try:
                    d["steps"] = json.loads(d.get("steps") or "[]")
                except ValueError:
                    d["steps"] = []
                try:
                    d["fingerprint"] = json.loads(d.get("fingerprint") or "{}")
                except ValueError:
                    d["fingerprint"] = {}
                if not isinstance(d["fingerprint"], dict):
                    d["fingerprint"] = {}
                rows.append(d)
            return rows
    except sqlite3.OperationalError:
        return []


def agent_record_stats(root: str, agent: str, days: int = 30) -> dict:
    """近 N 天该 agent 的工作记录数 + 最近一条日期（接入是否真在用）。"""
    key = _norm_agent(agent)
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    try:
        with db_conn(root) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(date) AS last FROM records "
                "WHERE lower(agent)=? AND status='active' AND date>=?", (key, since)).fetchone()
            return {"days": days, "count": row["n"] or 0, "last": row["last"] or ""}
    except sqlite3.Error:
        return {"days": days, "count": 0, "last": ""}


SCHEMA_VERSION = 2
# B11 schema 版本号。约定：新增表/列/迁移时 **+1**，并在本注释里记一笔：
#   v1 = 基线（本登记落地的时刻；此前所有迁移都靠幂等的 _migrate_columns 逐列 PRAGMA 试探，
#        能补列但没有版本号——无法回答「这个库现在是哪个版本」「是不是降级运行了」）。
#   v2 = R6 新增 agent_onboarding（接入验证快照）。
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

def _migration_fingerprint(project: str, title: str, body: str) -> tuple:
    """迁移条目的稳定身份：项目 + 标题 + 正文全文哈希。

    刻意**不含行号/文件名**：行号在文件顶部插入横幅/章节时会整体位移，旧实现把它当身份，
    2026-10-21 实测因 4 行横幅一次性多出 32 条假记录；文件名变更同样不该让旧内容复活。
    标题和正文都参与：正文修订 = 文档内容真的变了，应作为新条目导入（旧条目保留可追溯）。
    """
    import hashlib
    h = hashlib.sha1(f"{title or ''}\x00{body or ''}".encode("utf-8")).hexdigest()
    return (project, h)


def _migrate_records(root: str) -> int:
    """扫描全部项目目录导入记录；所有非保留目录登记进 projects。返回本次导入条数。

    对齐 core.scan 的记录提取语义：
    - 有 工作记录.md 用之；否则用目录下最新 md/txt 顶上，整篇无段头按 "[主文档]" 一条
    - 段头/段内解析不出日期时用文档 mtime 兜底（fallback_date）
    幂等：先清掉 date 为空的**活跃**旧迁移记录（早期版本未传 fallback_date 的缺日期数据；
    软删行是墓碑，必须保留参与去重，否则下一次会复活），再按内容指纹增量导入，可安全重复跑。
    去重键含正文全文：同一项目里存在段头完全相同的多条段（如同名 "## 日期（agent）"），
    只按段头去重会吞掉第二条的内容。"""
    n = 0
    with db_conn(root) as conn:
        conn.execute("DELETE FROM records WHERE source LIKE 'migrated:%' AND date='' "
                     "AND status='active'")
        # ★ 去重身份 = (project, sha1(title + 正文全文))，**不含行号、文件名、agent、date**：
        #   · 不含行号：顶部插横幅/章节会让后续行号整体位移，旧 key 会把同一段文字当新记录
        #     重插（2026-10-21 实测 32 条假记录，项目最后活动日被污染）。
        #   · 不含文件名：同内容换文件名（INDEX→其他）不该复活历史条目。
        #   · 不含 agent：agent 是派生值，会随解析规则改进变化。
        #   · 不含 date：无段内日期的条目用文件 mtime 兜底，追加记录会改 mtime。
        #   · 查询不过滤 status：软删行是墓碑，必须继续参与去重（"失效≠删除"）。
        known = set()
        for r in conn.execute(
                "SELECT project,title,content FROM records WHERE source LIKE 'migrated:%'"):
            known.add(_migration_fingerprint(r["project"], r["title"], r["content"] or ""))
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
                # key 必须与上面 known 的元素**同结构**（内容指纹，不含行号/文件名）。
                key = _migration_fingerprint(entry.name, r.title, r.body or "")
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


# ---------------------------------------------------------------- R48 全局写入冻结
# 事故时最缺的不是「再写一条记录」，而是「先让所有人停止写」：多 agent 并行下，
# 一个人修库、其他人继续写，会把现场搅成无法对账（外部事故复盘的共同结论）。
# 冻结是写面闸门：MCP 层拦写工具，读工具照常；hub_freeze/hub_undo 例外——
# 解冻和修复不能把自己锁在外面。状态落 meta，跨进程即时生效；定时冻结自动过期。

FREEZE_KEY = "write_freeze"
FREEZE_MAX_MINUTES = 24 * 60


def _freeze_read(conn) -> dict:
    out = {"active": False, "until": "", "reason": "", "by": "", "at": "",
           "remaining_min": 0.0, "expired": False, "minutes": 0}
    row = conn.execute("SELECT value FROM meta WHERE key=?", (FREEZE_KEY,)).fetchone()
    if not row or not (row["value"] or "").strip():
        return out
    try:
        d = json.loads(row["value"])
        if not isinstance(d, dict):
            raise ValueError("not a dict")
    except (ValueError, TypeError):
        # 状态损坏按冻结处理（fail-closed）：冻结是安全闸门，坏数据不能等于自动解冻。
        return out | {"active": True,
                      "reason": "（冻结状态损坏，按冻结处理；用 hub_freeze unfreeze 解除）"}
    until = str(d.get("until") or "").strip()
    try:
        minutes = int(d.get("minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    out.update({"until": until, "reason": str(d.get("reason") or ""),
                "by": str(d.get("by") or ""), "at": str(d.get("at") or ""),
                "minutes": minutes})
    if not until:
        out["active"] = True  # 持续冻结型（直到手动解除）
        return out
    try:
        end = datetime.datetime.strptime(until, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        out.update({"active": True, "reason": out["reason"] + "（时间损坏，按冻结处理）"})
        return out
    rem = (end - datetime.datetime.now()).total_seconds() / 60
    if rem <= 0:
        out.update({"active": False, "expired": True, "remaining_min": 0.0})
    else:
        out.update({"active": True, "remaining_min": rem})
    return out


def freeze_get(root: str) -> dict:
    """当前全局写入冻结状态（只读；过期条目保留原样，active=False+expired=True）。"""
    try:
        with db_conn(root) as conn:
            return _freeze_read(conn)
    except sqlite3.Error:
        return {"active": False, "until": "", "reason": "", "by": "", "at": "",
                "remaining_min": 0.0, "expired": False, "minutes": 0}


def freeze_set(root: str, minutes: int = 0, reason: str = "", agent: str = "") -> tuple:
    """冻结全局写入。minutes=0 持续冻结直到手动解除；1..1440 定时自动解冻。返回 (错误或 "", 状态)。"""
    reason = (reason or "").strip()
    if not reason:
        return "reason 必填（冻结是全库写闸门，必须写清为什么，解冻时按它对账）", {}
    try:
        minutes = int(minutes or 0)
    except (TypeError, ValueError):
        return "minutes 必须是整数（0=持续冻结直到手动解除，1..1440=定时解冻）", {}
    if minutes < 0 or minutes > FREEZE_MAX_MINUTES:
        return f"minutes 只能是 0（持续冻结）或 1..{FREEZE_MAX_MINUTES}（收到 {minutes}）", {}
    now = datetime.datetime.now()
    until = "" if minutes == 0 else (
        now + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")
    payload = json.dumps({"until": until, "reason": reason[:300],
                          "by": _norm_agent(agent) or "user",
                          "at": now.strftime("%Y-%m-%dT%H:%M:%S"),
                          "minutes": minutes}, ensure_ascii=False)
    with db_conn(root) as conn:
        prev = _freeze_read(conn)
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (FREEZE_KEY, payload))
        _journal_row(conn, agent, "延长写入冻结" if prev["active"] else "写入冻结", "global",
                     f"{reason[:80]}｜{'持续冻结' if not until else '至 ' + until}")
    return "", freeze_get(root) | {"_was_active": prev["active"]}


def freeze_clear(root: str, agent: str = "", reason: str = "") -> tuple:
    """解除全局写入冻结（幂等）。返回 (错误或 "", 状态)。"""
    with db_conn(root) as conn:
        prev = _freeze_read(conn)
        conn.execute("DELETE FROM meta WHERE key=?", (FREEZE_KEY,))
        if prev["active"] or prev["expired"]:
            note = (reason or "手动解除").strip()[:120]
            if prev["expired"]:
                note += "（解除时已过期）"
            _journal_row(conn, agent, "解除写入冻结", "global", note)
    return "", freeze_get(root) | {"_was_active": prev["active"],
                                   "_was_expired": prev["expired"]}


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
        for tbl in ("records", "memories", "errors"):
            conn.execute(f"UPDATE {tbl} SET agent=lower(agent) "
                         "WHERE agent IS NOT NULL AND agent!=lower(agent)")
        # R47：已封存的流水行不能再改——改 agent 会让人永久的审计链当场断裂。
        # 新写入在 _journal_append 里已经归一，这里只收尾历史未封存行。
        conn.execute("UPDATE journal SET agent=lower(agent) "
                     "WHERE agent IS NOT NULL AND agent!=lower(agent) "
                     "AND (hash IS NULL OR hash='')")
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
        # 先拿写锁再 PRAGMA：两个新版本进程同时首启时，都读到缺列再先后 ALTER，
        # 后一个会以 duplicate column 失败——检查与补列必须串行（R15 同型竞态）。
        conn.execute("BEGIN IMMEDIATE")
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
        # 第五轮 R2（2026-10-20）复核列：新鲜度状态机自 R4 起就在读 confirmed，
        # 但列和写入路径一直缺失（只能靠 last_hit 弱确认）。补列纯增量，旧行默认空。
        if "confirmed" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN confirmed TEXT DEFAULT ''")
        # P1-1（2026-10-11）records 双时相版本列：valid_from/valid_until/superseded_by
        # 「失效 ≠ 删除」——旧版本行保留可追溯，检索只认现行有效（_rec_live_sql）。
        rcols = {r["name"] for r in conn.execute("PRAGMA table_info(records)")}
        for col, ddl in (("valid_from", "TEXT DEFAULT ''"),
                         ("valid_until", "TEXT DEFAULT ''"),
                         ("superseded_by", "INTEGER DEFAULT 0"),
                         ("task_id", "INTEGER DEFAULT 0")):
            if col not in rcols:
                conn.execute(f"ALTER TABLE records ADD COLUMN {col} {ddl}")
        # R35（2026-10-23）记录↔任务绑定索引。必须补列之后再建：旧库在 executescript
        # 阶段还没有 task_id，把这条写进 SCHEMA 会让升级首启直接报 no such column。
        conn.execute("CREATE INDEX IF NOT EXISTS idx_records_task ON records(task_id)")
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
        # R261 错误重复计数：同一 (agent, 项目, 标题) 的 open 错误再发生时累加而不是新增行
        ecols = {r["name"] for r in conn.execute("PRAGMA table_info(errors)")}
        for col, ddl in (("repeat_count", "INTEGER DEFAULT 1"), ("last_ts", "TEXT DEFAULT ''"),
                         ("last_detail", "TEXT DEFAULT ''")):
            if col not in ecols:
                conn.execute(f"ALTER TABLE errors ADD COLUMN {col} {ddl}")
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        for col, ddl in (("priority", "INTEGER DEFAULT 2"),
                         ("deps", "TEXT DEFAULT ''"),
                         ("due", "TEXT DEFAULT ''"),
                         ("deliverables", "TEXT DEFAULT ''"),
                         ("skills", "TEXT DEFAULT ''"),
                         ("cancel_reason", "TEXT DEFAULT ''"),
                         ("cancelled_at", "TEXT DEFAULT ''"),
                         ("cancelled_by", "TEXT DEFAULT ''"),
                         ("attempts", "INTEGER DEFAULT 0"),
                         ("next_retry_at", "TEXT DEFAULT ''"),
                         ("last_error", "TEXT DEFAULT ''"),
                         ("defer_until", "TEXT DEFAULT ''"),
                         ("defer_reason", "TEXT DEFAULT ''"),
                         ("deferred_at", "TEXT DEFAULT ''"),
                         ("approval_required", "INTEGER DEFAULT 0"),
                         ("approved_by", "TEXT DEFAULT ''"),
                         ("approved_at", "TEXT DEFAULT ''"),
                         ("rejected_by", "TEXT DEFAULT ''"),
                         ("rejected_at", "TEXT DEFAULT ''"),
                         ("reject_note", "TEXT DEFAULT ''"),
                         ("reject_count", "INTEGER DEFAULT 0")):
            if col not in tcols:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
        # R36（2026-10-23）agent 管控状态：active 正常 / draining 收尾不接新活 /
        # paused 暂停 / offline 离线。只增列，心跳不覆盖——停用是人的决定，不能被
        # 一次 heartbeat 悄悄复活。R37：skills 能力声明（逗号分隔）。
        acols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)")}
        for col, ddl in (("control_state", "TEXT DEFAULT 'active'"),
                         ("control_note", "TEXT DEFAULT ''"),
                         ("control_at", "TEXT DEFAULT ''"),
                         ("skills", "TEXT DEFAULT ''"),
                         ("role", "TEXT DEFAULT 'worker'")):
            if col not in acols:
                conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {ddl}")
        # R26（2026-10-22）进展时间戳：心跳续租同样刷新 updated，拿 updated 当「任务在走」
        # 的证据会让「人在线、活停住」永远不可见。progress_at 只由真实任务动作写
        # （认领/流转/改详情/写检查点），心跳不碰；老行以迁移时的 updated 起步，此后靠
        # 新写入自愈（迁移前心跳污染过的值最多让首次停滞发现晚一轮）。
        if "progress_at" not in tcols:
            conn.execute("ALTER TABLE tasks ADD COLUMN progress_at TEXT DEFAULT ''")
            conn.execute("UPDATE tasks SET progress_at=updated "
                         "WHERE progress_at IS NULL OR progress_at=''")
        # R6（2026-10-21）接入验证快照补指纹列：旧快照默认空，读取时回落 mtime 判定
        # R23（2026-10-22）再补 base 列：记录指纹来自哪个代码树，跨树比对不判过期
        acols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_onboarding)")}
        if acols and "fingerprint" not in acols:
            conn.execute("ALTER TABLE agent_onboarding ADD COLUMN fingerprint TEXT DEFAULT ''")
        if acols and "base" not in acols:
            conn.execute("ALTER TABLE agent_onboarding ADD COLUMN base TEXT DEFAULT ''")
        # R89（2026-10-24）交接作废出口：废弃的 open 交接可关闭，必须留作废人与原因；
        # 只加列，历史行默认空，不影响既有 open/claimed 语义。
        hcols = {r["name"] for r in conn.execute("PRAGMA table_info(handoffs)")}
        for col, ddl in (("closed_by", "TEXT DEFAULT ''"),
                         ("closed_at", "TEXT DEFAULT ''"),
                         ("close_note", "TEXT DEFAULT ''")):
            if col not in hcols:
                conn.execute(f"ALTER TABLE handoffs ADD COLUMN {col} {ddl}")
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
        # R47（2026-10-23）审计链：流水行带 prev_hash/hash，改字段/删行/调序都会在验证时暴露。
        # 首次升级把存量流水按 id 顺序封存（meta.journal_chain_at 记封存时点）——
        # 封存前的历史只能证明「封存后未被改」，报告里如实标注，不假装有全生命周期证据。
        jcols = {r["name"] for r in conn.execute("PRAGMA table_info(journal)")}
        if "hash" not in jcols:
            if "prev_hash" not in jcols:
                conn.execute("ALTER TABLE journal ADD COLUMN prev_hash TEXT DEFAULT ''")
            conn.execute("ALTER TABLE journal ADD COLUMN hash TEXT DEFAULT ''")
            _journal_seal_backlog(conn)
        elif "prev_hash" not in jcols:
            conn.execute("ALTER TABLE journal ADD COLUMN prev_hash TEXT DEFAULT ''")


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
               review_date: str = "", task_id: int = 0) -> int:
    """写一条工作记录（hub 入口）。自动登记项目（历史目录迁移时已登记），并刷新项目活跃状态。
    source：hub=人工经 MCP 写入；auto=会话边界 hook 自动捕获（未经蒸馏，2.1）。
    决策字段（3.2 AgDR，可选）：context/options/decision/tradeoffs 传任意非空值即在
    正文尾部追加「## 决策记录」段；全不传时行为与旧版完全一致。
    record_type（P1-2，可选）：work_log/decision/todo/note；缺省按内容推断（含决策段→decision）。
    review_date（M11，可选）：YYYY-MM-DD——到期后此决策进「待验证」队列（心跳推送+回顾工具），
    传了才建（不传不建，零行为变化）。
    task_id（R35，可选）：绑定到任务——结晶优先收编绑定记录，巡检可核对「记录称已完成、
    任务却还开着」的闭环漂移；0=不绑定（旧行为）。"""
    source = source if source in ("hub", "auto") else "hub"
    try:
        task_id = max(0, int(task_id or 0))
    except (TypeError, ValueError):
        task_id = 0
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
            "INSERT INTO records(project,agent,date,title,content,source,created,valid_from,"
            "record_type,task_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (project, agent, date, title, content, source, _now(), (date or _now()[:10]), rtype,
             task_id))
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


def recent_diverse(root: str, limit: int = 8, pool: int = 200) -> list:
    """总览「最近动态」的项目多样性采样（R122）：纯时间序 limit=8 在「单项目密集
    迭代」时全是同一个项目的同 agent 记录（真实库实测 9 行全是 AgentHub 刷屏），
    看不出「电脑上都在干什么」。第一遍按项目去重取最新；不足再按时间序回填补齐。
    返回 ≤limit 条（date DESC 保持不变，只调整入选）。"""
    lim = max(1, min(int(limit), 50))
    rows = list_records(root, limit=max(lim * 4, int(pool)))
    seen: set = set()
    out = []
    for r in rows:
        p = r.get("project") or ""
        if p in seen:
            continue
        seen.add(p)
        out.append(r)
        if len(out) >= lim:
            return out
    for r in rows:  # 项目数不足 limit：回填其余最新（时间序）
        if r not in out:
            out.append(r)
            if len(out) >= lim:
                break
    return out


def records_agents(root: str) -> list:
    """records 出现过的 agent（小写去重；虚拟滚动模式填过滤器下拉用）。

    R778：`SELECT DISTINCT lower(agent)` 让 SQLite 建临时 B 树做 DISTINCT，20 万记录库
    实测 87ms（覆盖索引能供 status 条件，但表达式结果不在索引里）；改取原始值在 Python
    去重小写后 15.2ms。返回语义不变：小写、排序、空排除。"""
    with db_conn(root) as conn:
        rows = conn.execute(
            "SELECT DISTINCT agent FROM records "
            "WHERE status='active' AND agent<>''").fetchall()
    return sorted({r[0].lower() for r in rows})


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

_DF_MEMO = threading.local()   # R298：单次 search_all 内的 df 复用（见 _df_memo_scope）


@contextmanager
def _df_memo_scope():
    """一次 search_all 内复用 _token_stats 结果（R298，线程本地、退出即弃）。

    search_records 与 search_memories 各调一次 _coverage_gate，kw 相同 → 同一批 token
    的 df 被整表扫两遍（真实库实测 33ms/次）。只做「同一次查询内」复用：df 是精度
    启发式（fail-open），但不该跨查询缓存——别的进程随时在写，缓存会让 idf 悄悄过期。
    """
    prev = getattr(_DF_MEMO, "cache", None)
    _DF_MEMO.cache = {}
    try:
        yield
    finally:
        _DF_MEMO.cache = prev


def _token_stats(root: str, tokens: list) -> tuple:
    """一次连接内算完 (每个 token 的活跃行文档频率, 活跃语料行数)。

    R236：每表**一次扫描**用 SUM(content LIKE ?) 条件聚合出全部 token 的 df。旧写法每
    token 发两条 COUNT 全表扫（query 16 个 token = 32 次扫），真实库单次检索的 df 段
    34–82ms、占一次 search_memories 的一半——体检的 5 条泄漏探针因此把 stale_health 顶成
    耗时榜首。同口径下逐 token 与旧写法严格相等（tests/unit/test_token_df_r236.py 守住）。
    口径修正：旧实现只写了「只算活跃行」的承诺、SQL 却没带 live 谓词——被 superseded/作废
    的行会虚增 df、压低 idf，让「常见词蹭命中」更容易过关。
    fail-open：读不了库返回 ({}, 0)，闸门退化为纯计数（见 _coverage_gate）。
    """
    tokens = list(dict.fromkeys(tokens or []))   # 必须先先去重：批量写法是累加，重复词会翻倍
    df: dict = {t: 0 for t in tokens}
    if not tokens:
        return df, 0
    memo = getattr(_DF_MEMO, "cache", None)
    key = (str(root), tuple(tokens)) if memo is not None else None
    if key is not None and key in memo:
        c_df, c_n = memo[key]
        return dict(c_df), c_n
    try:
        with db_conn(root) as conn:
            for tbl, live in (("records", _rec_live_sql()), ("memories", _live_sql())):
                for i in range(0, len(tokens), 64):    # 参数个数与表达式深度留余量
                    chunk = tokens[i:i + 64]
                    cols = ", ".join("SUM(content LIKE ? ESCAPE '\\')" for _ in chunk)
                    args = tuple(f"%{_like_escape(t)}%" for t in chunk)
                    row = conn.execute(f"SELECT {cols} FROM {tbl} WHERE {live}", args).fetchone()
                    for t, v in zip(chunk, row or ()):
                        df[t] = df.get(t, 0) + int(v or 0)
            n_total = (conn.execute("SELECT COUNT(*) FROM records WHERE " + _rec_live_sql()).fetchone()[0]
                       + conn.execute("SELECT COUNT(*) FROM memories WHERE " + _live_sql()).fetchone()[0])
    except Exception:  # noqa: BLE001 闸门是精度优化，读不了库就不加权
        return {}, 0
    if key is not None:
        memo[key] = (dict(df), n_total)
    return df, n_total


def _token_df(root: str, tokens: list) -> dict:
    """query token 在 records+memories 活跃行的文档频率（口径与实现见 _token_stats）。

    常见 bigram（存在/在的）df 高 → idf 低，纯计数覆盖治不了「蹭两个常见词冒充强命中」
    （R4 实测：废话查询靠 不存+存在+在的 三个常见词凑满覆盖度）。
    """
    return _token_stats(root, tokens)[0]


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


def _latin_garbage_tokens(root: str, tokens: list, df: dict | None = None) -> list:
    """零命中的**拉丁** token（qwerty/zzz/xyzzy）——乱输/拼错的强信号。

    与 CJK 词缺失的区别：「关掉」在库里没有（库用「关闭」）是说法稀缺，不是垃圾；
    而随机拉丁串在任何语料里都不该有。判据：token 纯 ASCII 且两表 df=0。
    fail-open：读不了库返回 []（不闸）。
    df 可由调用方传入（_coverage_gate 已算过）——每 token 两条全表 COUNT，
    在 3 万记录库实测占一次检索的 ~15%，没有理由重复扫。
    """
    df = df if df else _token_df(root, tokens)
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
    df, n_total = _token_stats(root, q_tokens)
    idf: dict = {}
    total_mass = 1.0
    if df:
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
    if _latin_garbage_tokens(root, q_tokens, df):
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
    # df 只统计 query 词：原实现对每篇文档的全部词表做 df.update（真实库记忆 5.5ms、
    # 记录 25ms，占打分 99%），而 df 只在 w∈q_set 时才被读——等价改写（R250 实测 0 差异）。
    df: Counter = Counter()
    for _, tf, _dl in docs:
        for w in q_set:
            if w in tf:
                df[w] += 1
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
    空格拆词对中文整串无效，正是本通道要治的病。返回 [{**row, "_bm25": 分数}]。

    R250：窄列取行（id+分词三列）打分，只对命中的 top-k 回表取全列——输出字段与
    顺序不变（真实库逐字段对拍 0 差异），记忆省 0.7ms、记录省 2.9ms。回表仍带
    _live_sql：若某行在两次查询之间被取代/过期，它从结果里消失，等同早一瞬取数。"""
    q_set = list(dict.fromkeys(_tokenize(query)))
    if not q_set:
        return []
    sql = "SELECT id,content,tags,project FROM memories WHERE " + _live_sql() + ""
    args: list = []
    if kind in KINDS:
        sql += " AND kind=?"
        args.append(kind)
    with db_conn(root) as conn:
        rows = conn.execute(sql, args).fetchall()
    if not rows:
        return []
    # 语料本身装不下缓存时不缓存：每行都会被每条检索摸到，FIFO/LRU 都必然抖动
    # （3 万行 + 2 万上限实测 0.3s → 5.7s）。宁可每次重新分词，也不做「越用越慢」的缓存。
    use_cache = _TOK_CACHE_MAX <= 0 or len(rows) <= _TOK_CACHE_MAX
    docs = []
    for r in rows:
        ent = _TOK_CACHE.get_mem_bm25(root, r["id"]) if use_cache else None
        if ent is None:
            toks = _tokenize(" ".join([r["content"] or "", r["tags"] or "", r["project"] or ""]),
                             max_tokens=2000)  # doc 侧不截断：截了长记忆后半无法命中（复盘实锤）
            ent = (Counter(toks), len(toks))
            if use_cache:
                _TOK_CACHE.put_mem_bm25(root, r["id"], ent)
        docs.append((r, ent[0], ent[1]))
    ranked = _bm25_score(q_set, docs)[:max(0, min(int(limit), 100))]
    if not ranked:
        return []
    ids = [r["id"] for _s, r in ranked]
    with db_conn(root) as conn:
        full = {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM memories WHERE " + _live_sql()
            + " AND id IN (" + ",".join("?" * len(ids)) + ")", ids)}
    return [dict(full[r["id"]], _bm25=round(s, 4)) for s, r in ranked if r["id"] in full]


class _TokenCache:
    """进程内分词缓存统一层（R11，2026-10-18 重构）——终结「三个键同型的裸 dict 靠注释区分」。

    由来（R6 踩坑，记忆 #219）：BM25 的 mem 缓存存 (Counter,dl)、内容含 tags+project；
    相似度缓存存 set、只用 content。我曾因「键都是 (root,id)」判定可复用，set 存进前者后
    BM25 读到就 AttributeError——**键同型不等于缓存可共用**，值与内容语义都得看。
    裸 dict + 注释的防线依赖「下一个人真的读注释」；重构为 typed 门面后，三个命名空间
    外部不可见，只能经各自的 get/put 访问，从结构上不可能串味。

    三个命名空间（注意 mem_bm25 与 records_bm25 的 **id 空间互相独立**——
    memory #5 与 record #5 是不同实体，所以必须是不同命名空间而非同 dict 加前缀）：
      mem_bm25     (root, memory_id) → (Counter, dl)  记忆 BM25，content+tags+project
      records_bm25 (root, record_id) → (Counter, dl)  记录 BM25，title+content+project
      sim          (root, memory_id) → set            相似度，仅 content

    失效：invalidate(root, memory_id) 一并 pop mem_bm25 + sim——edit_memory 的
    text_changed 分支是唯一调用点（改一处=两个命名空间都失效，不会漏）。
    records 内容只增不改，不设失效入口（record 的 supersede 是行级软删、不进分词缓存键）。
    key 必须含 root：不同库的 id 空间独立（adv 多 tmp 库实测串库：id=1 在各库是不同内容）。
    """
    __slots__ = ("_mem_bm25", "_records_bm25", "_sim")

    def __init__(self) -> None:
        self._mem_bm25: dict = {}
        self._records_bm25: dict = {}
        self._sim: dict = {}

    # ---- mem_bm25：(Counter, dl)
    def get_mem_bm25(self, root: str, mid: int):
        return self._mem_bm25.get((root, mid))

    def put_mem_bm25(self, root: str, mid: int, entry) -> None:
        self._mem_bm25[(root, mid)] = entry
        self._trim(self._mem_bm25)

    # ---- records_bm25：(Counter, dl)
    def get_records_bm25(self, root: str, rid: int):
        return self._records_bm25.get((root, rid))

    def put_records_bm25(self, root: str, rid: int, entry) -> None:
        self._records_bm25[(root, rid)] = entry
        self._trim(self._records_bm25)

    @staticmethod
    def _trim(d: dict) -> None:
        """FIFO 上限：多根共享缓存时兜底，超了从最早插入的开始丢。

        为什么必须有：常驻 MCP 进程的缓存没有生命周期，3 万条记录实测 ~57MB
        （每条 Counter 约 2KB，随正文长度增长）。单库语料超过上限时调用方根本
        不写入（见 _bm25_rank/_records_bm25_rank，避免逐条淘汰抖动）；
        这里只处理「多个小库共用进程」的总量。环境变量 AGENTHUB_TOK_CACHE_MAX
        可调，0=不限。
        """
        cap = _TOK_CACHE_MAX
        if cap > 0 and len(d) > cap:
            for k in list(d)[: len(d) - cap]:
                d.pop(k, None)

    # ---- sim：set（相似度专用，与上面两个 BM25 命名空间结构隔离）
    def get_sim(self, root: str, mid: int):
        return self._sim.get((root, mid))

    def put_sim(self, root: str, mid: int, toks: set) -> None:
        # R244：此前只有 mem_bm25 走 _trim，sim 命名空间无上限。similar_memories 全库扫描
        # 会一次性写入每条现行记忆，常驻进程里同样需要总量兜底（理由见 _trim）。
        self._sim[(root, mid)] = toks
        self._trim(self._sim)

    # ---- 失效（单一入口）：记忆文本改了，BM25 与相似度两个命名空间一起作废
    def invalidate(self, root: str, mid: int) -> None:
        self._mem_bm25.pop((root, mid), None)
        self._sim.pop((root, mid), None)

    def sizes(self) -> tuple:
        """三命名空间条目数（回归/诊断用）。"""
        return len(self._mem_bm25), len(self._records_bm25), len(self._sim)

    def clear(self) -> None:
        """三命名空间全清（回归隔离用——测试间不共享缓存状态）。"""
        self._mem_bm25.clear()
        self._records_bm25.clear()
        self._sim.clear()

    def sim_items(self) -> dict:
        """sim 命名空间 (key → set) 快照（回归断言用，只读副本）。"""
        return dict(self._sim)

    def mem_bm25_items(self) -> dict:
        """mem_bm25 命名空间 (key → (Counter, dl)) 快照（回归断言用，只读副本）。"""
        return dict(self._mem_bm25)


try:
    _TOK_CACHE_MAX = max(0, int(os.environ.get("AGENTHUB_TOK_CACHE_MAX", "40000") or 0))
except ValueError:  # 环境变量写坏不能拖垮整个大脑导入
    _TOK_CACHE_MAX = 40000

_TOK_CACHE = _TokenCache()
# 1559 条全文分词 ~300ms/查询（搜索页同步调用，用户抱怨过慢）→ 缓存后首查之外 <10ms


def _records_bm25_rank(root: str, query: str, limit: int = 30) -> list:
    """records 表的 BM25 通道（P4.3b）：匹配面 title+content+project（与 _records_query 一致）。
    记录正文长（数百字）且无 updated 列——recency 不适用（β 全局关闭中）。返回带 _bm25 分。
    R250：窄列取行 + top-k 回表，做法与理由同 _bm25_rank（记录 1594 行省 2.9ms）。"""
    q_set = list(dict.fromkeys(_tokenize(query)))
    if not q_set:
        return []
    with db_conn(root) as conn:
        rows = conn.execute("SELECT id,title,content,project FROM records WHERE "
                            + _rec_live_sql()).fetchall()
    if not rows:
        return []
    use_cache = _TOK_CACHE_MAX <= 0 or len(rows) <= _TOK_CACHE_MAX   # 理由同 _bm25_rank
    docs = []
    for r in rows:
        ent = _TOK_CACHE.get_records_bm25(root, r["id"]) if use_cache else None
        if ent is None:
            toks = _tokenize(" ".join([r["title"] or "", r["content"] or "", r["project"] or ""]),
                             max_tokens=2000)
            ent = (Counter(toks), len(toks))
            if use_cache:
                _TOK_CACHE.put_records_bm25(root, r["id"], ent)
        docs.append((r, ent[0], ent[1]))
    ranked = _bm25_score(q_set, docs)[:max(0, min(int(limit), 100))]
    if not ranked:
        return []
    ids = [r["id"] for _s, r in ranked]
    with db_conn(root) as conn:
        full = {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM records WHERE " + _rec_live_sql()
            + " AND id IN (" + ",".join("?" * len(ids)) + ")", ids)}
    return [dict(full[r["id"]], _bm25=round(s, 4)) for s, r in ranked if r["id"] in full]


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
    sem_rows = _semantic_rank(root, kw, "records") if _RRF_W_SEMANTIC_RECORDS > 0 else []  # P0-3 语义第三路
    for eid, s, _ in sem_rows:
        scores[eid] = scores.get(eid, 0.0) + _RRF_W_SEMANTIC_RECORDS * s
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


def audit_count(root: str) -> int:
    """写操作审计总数（R276）：audit_list 有 LIMIT，抬头不能把「取到的条数」当总数。"""
    with db_conn(root) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM op_audit").fetchone()[0])


def log_tool_call(root: str, tool: str, agent: str = "",
                  channel: str = "") -> None:
    """C3→D6/R6：工具调用留痕。**全部工具**都记（R6 扩面前只有 4 个能力型只读工具记，
    导致「48/69 从未被调用」绝大部分是留痕盲区的假象——拿假象治理会把在用工具当僵尸下线）。
    fail-open：留痕失败绝不影响调用本身。写工具另走 op_audit（审计语义含 undo 句柄），
    与这里的调用计数不可互相替代（一条写调用两边都有记录，是故意的：两个问题两套答案）。
    channel（R112）：mcp=会话调用（session=True）/ admin=管理通道（CLI/GUI/REST——
    空归属在这里是**设计内**，usage_health 不再把它当归属缺口提示）；历史行 ''。"""
    try:
        with db_conn(root) as conn:
            _ensure_tool_calls_channel(conn)
            conn.execute(
                "INSERT INTO tool_calls(ts, agent, tool, channel) VALUES(?,?,?,?)",
                (_now(), _norm_agent(agent) or "", tool,
                 channel if channel in ("mcp", "admin") else ""))
    except Exception:  # noqa: BLE001
        pass


def _ensure_tool_calls_channel(conn) -> None:
    """R112 惰性迁移：tool_calls 加 channel 列（历史行保留 ''，不猜）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tool_calls)")}
    if "channel" not in cols:
        conn.execute("ALTER TABLE tool_calls ADD COLUMN channel TEXT NOT NULL DEFAULT ''")


# ---------------------------------------------------------------- D10/R4：使用时间线
# 为什么做：审计日志本身不是可观测性——「对日志的定时查询才是」（moltbook 生产实录：
# 5 条固定查询在事故前就把模式捞出来）。本大脑有四张使用表（tool_calls 全工具调用 /
# op_audit 写审计 / searches 检索 / journal 运维动作），此前面上没有统一查询：
# 「这个工具最近谁在用什么程度」要分别查四张表再人脑合并——D10 遗留项的正式交付。
# 边界诚实（usage_report 同款纪律）：只反映 MCP 面；GUI 直接调 brain.py 不入库。

def _usage_cutoff(days: int) -> str:
    """窗口起点（ISO 秒级）。days<=0 → 空串（调用方视为不过滤）。"""
    if days <= 0:
        return ""
    return (datetime.datetime.now() - datetime.timedelta(days=int(days))).isoformat(timespec="seconds")


def usage_timeline(root: str, days: int = 7, agent: str = "", tool: str = "",
                   limit: int = 50) -> list:
    """四源统一使用时间线（倒序）。每条事件带 source 字段区分来路：
    call=tool_calls（全工具调用）/ audit=op_audit（写操作）/ search=searches（检索）/ journal（运维动作）。
    tool 过滤只作用于三个带 tool 列的源；journal 是运维动作流（action/target，无 tool 列），
    tool 过滤时不计入——如实标注好过静默漏掉。agent 过滤四源通用（规范化小写比对）。"""
    cutoff = _usage_cutoff(days)
    ag = _norm_agent(agent)
    tl = tool.strip()
    ev: list = []
    with db_conn(root) as conn:
        q = ("SELECT id, ts, agent, tool FROM tool_calls WHERE 1=1"
             + (" AND replace(substr(ts,1,19),' ','T')>=?" if cutoff else "")
             + (" AND agent=?" if ag else "") + (" AND tool=?" if tl else ""))
        args: list = [x for x in (cutoff, ag, tl) if x]
        for r in conn.execute(q + " ORDER BY id DESC LIMIT 400", args):
            ev.append({"ts": r["ts"], "source": "call", "agent": r["agent"] or "",
                       "subject": r["tool"], "detail": "", "id": r["id"]})
        q = ("SELECT id, ts, agent, tool, project, summary, undo_kind, undo_ref FROM op_audit WHERE 1=1"
             + (" AND replace(substr(ts,1,19),' ','T')>=?" if cutoff else "")
             + (" AND agent=?" if ag else "") + (" AND tool=?" if tl else ""))
        for r in conn.execute(q + " ORDER BY id DESC LIMIT 400", args):
            undo = f" · undo={r['undo_kind']}#{r['undo_ref']}" if r["undo_ref"] else ""
            proj = f" · {r['project']}" if r["project"] else ""
            ev.append({"ts": r["ts"], "source": "audit", "agent": r["agent"] or "",
                       "subject": r["tool"], "detail": f"{(r['summary'] or '')[:70]}{proj}{undo}",
                       "id": r["id"]})
        if not tl:  # journal 无 tool 列：tool 过滤时整体不计入（见 docstring）
            q = ("SELECT id, ts, agent, action, target, note FROM journal WHERE 1=1"
                 + (" AND replace(substr(ts,1,19),' ','T')>=?" if cutoff else "")
                 + (" AND agent=?" if ag else ""))
            jargs: list = [x for x in (cutoff, ag) if x]
            for r in conn.execute(q + " ORDER BY id DESC LIMIT 400", jargs):
                tgt = f" · {r['target']}" if r["target"] else ""
                note = f" · {(r['note'] or '')[:50]}" if r["note"] else ""
                ev.append({"ts": r["ts"], "source": "journal", "agent": r["agent"] or "",
                           "subject": r["action"], "detail": f"{tgt}{note}", "id": r["id"]})
        q = ("SELECT id, ts, agent, tool, query, hits FROM searches WHERE 1=1"
             + (" AND replace(substr(ts,1,19),' ','T')>=?" if cutoff else "")
             + (" AND agent=?" if ag else "") + (" AND tool=?" if tl else ""))
        for r in conn.execute(q + " ORDER BY id DESC LIMIT 400", args):
            ev.append({"ts": r["ts"], "source": "search", "agent": r["agent"] or "",
                       "subject": r["tool"], "detail": f"「{(r['query'] or '')[:50]}」→{r['hits']} 命中",
                       "id": r["id"]})
    ev.sort(key=lambda e: (e["ts"], e["id"]), reverse=True)
    return ev[:max(1, min(int(limit), 200))]


def usage_stats(root: str, days: int = 7, agent: str = "", tool: str = "") -> dict:
    """窗口内使用聚合（只读，不写任何数据）：
    · totals：四源各自条数（journal 在 tool 过滤时不计，口径同 usage_timeline）；
    · by_tool / by_agent：tool_calls 权威口径（R6 起全工具留痕）下的排行；
    · attribution：调用归属覆盖（有主/空/占比）——空归属高说明调用方没带
      --agent 或仍在跑未修复的旧进程，控制面数据可信度的直接指标；
    · last_seen：每工具最后露面时间——「零调用」断言的活校验（脚本快照会过期，这个不会）；
    · zero_hit_searches：窗口内零命中检索——**反复出现的零命中查询=索引/描述问题的信号**，
      比工具计数更早暴露「有需求没满足」；
    · repeated_queries：同 agent 同 query 出现 ≥3 次——moltbook 生产实录里 retry storm 的
      可计算代理（我们没有 args_hash，但 searches 有 query 原文，重复即信号）。
    注意：burst 类指标（同 agent 同工具高频）**不做**——tool_calls 无参数列，
    正常的高频只读（heartbeat/status 类）与异常重试在这张表上不可区分，做了就是噪声。"""
    cutoff = _usage_cutoff(days)
    ag = _norm_agent(agent)
    tl = tool.strip()
    out: dict = {"window_days": int(days) if days > 0 else 0, "totals": {},
                 "by_tool": [], "by_agent": [], "last_seen": {},
                 "attribution": {}, "zero_hit_searches": [], "repeated_queries": []}
    with db_conn(root) as conn:
        conds, args = [], []
        if cutoff:
            conds.append("replace(substr(ts,1,19),' ','T')>=?")
            args.append(cutoff)
        if ag:
            conds.append("agent=?")
            args.append(ag)
        if tl:
            conds.append("tool=?")
            args.append(tl)
        w = (" WHERE " + " AND ".join(conds)) if conds else ""
        out["totals"]["calls"] = conn.execute(
            f"SELECT COUNT(*) FROM tool_calls{w}", args).fetchone()[0]
        out["totals"]["audits"] = conn.execute(
            f"SELECT COUNT(*) FROM op_audit{w}", args).fetchone()[0]
        out["totals"]["searches"] = conn.execute(
            f"SELECT COUNT(*) FROM searches{w}", args).fetchone()[0]
        jc, ja = [], []
        if cutoff:
            jc.append("replace(substr(ts,1,19),' ','T')>=?")
            ja.append(cutoff)
        if ag:
            jc.append("agent=?")
            ja.append(ag)
        if not tl:  # journal 无 tool 列，tool 过滤时不计（口径同时间线）
            out["totals"]["journal"] = conn.execute(
                f"SELECT COUNT(*) FROM journal"
                + ((" WHERE " + " AND ".join(jc)) if jc else ""), ja).fetchone()[0]
        out["by_tool"] = [dict(r) for r in conn.execute(
            f"SELECT tool, COUNT(*) n FROM tool_calls{w} GROUP BY tool ORDER BY n DESC, tool LIMIT 30",
            args)]
        out["by_agent"] = [dict(r) for r in conn.execute(
            f"SELECT agent, COUNT(*) n FROM tool_calls{w} GROUP BY agent ORDER BY n DESC, agent LIMIT 30",
            args)]
        arow = conn.execute(
            f"SELECT COUNT(*) total,"
            f" SUM(CASE WHEN TRIM(COALESCE(agent,''))='' THEN 1 ELSE 0 END) empty"
            f" FROM tool_calls{w}", args).fetchone()
        atotal = int(arow["total"] or 0)
        aempty = int(arow["empty"] or 0)
        out["attribution"] = {
            "total": atotal, "named": atotal - aempty, "empty": aempty,
            "empty_pct": round(aempty * 100.0 / atotal, 1) if atotal else 0.0,
        }
        out["last_seen"] = {r["tool"]: r["last"] for r in conn.execute(
            f"SELECT tool, MAX(ts) last FROM tool_calls{w} GROUP BY tool ORDER BY tool", args)}
        zc, za = [], []
        if cutoff:
            zc.append("replace(substr(ts,1,19),' ','T')>=?")
            za.append(cutoff)
        if ag:
            zc.append("agent=?")
            za.append(ag)
        if tl:
            zc.append("tool=?")
            za.append(tl)
        zw = (" WHERE hits=0 AND query!=''" + (" AND " + " AND ".join(zc) if zc else ""))
        out["zero_hit_searches"] = [dict(r) for r in conn.execute(
            f"SELECT query, agent, COUNT(*) n, MAX(ts) last FROM searches{zw} "
            f"GROUP BY query, agent ORDER BY n DESC, last DESC LIMIT 15", za)]
        out["repeated_queries"] = [dict(r) for r in conn.execute(
            f"SELECT query, agent, COUNT(*) n FROM searches"
            + ((" WHERE query!=''" + (" AND " + " AND ".join(zc) if zc else "")) if zc else " WHERE query!=''")
            + " GROUP BY query, agent HAVING COUNT(*)>=3 ORDER BY n DESC LIMIT 15", za)]
    return out


def flush_index(root: str, force: bool = False, batch: int = 64) -> dict:
    """语义索引维护（P3-1 hub_flush_index）：把缺失（force=True 时全部）向量补进 embeddings。
    文本口径与写入钩子 _ensure_emb_for、tools/build_embeddings.py 三处一致。
    fail-open：语义依赖缺失/模型装载失败 → 返回 {"error": ...}，不影响其余功能。"""
    out = {"records": 0, "memories": 0, "scanned": 0, "dim": None, "model": _EMB_MODEL_NAME}
    if _st_cls() is None:
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

TASK_STATUSES = ("pending", "in_progress", "blocked", "done", "cancelled", "dead")
TASK_CN = {"pending": "待办", "in_progress": "进行中", "blocked": "阻塞", "done": "完成",
           "cancelled": "已取消", "dead": "死信"}
TASK_TERMINAL = ("done", "cancelled", "dead")  # R41/R43：终态不可逆（重开请建新任务）
CLAIM_TTL_HOURS = 2   # 认领超时自动释放（防死锁）：claimed_at 早于该时长的 in_progress 可被接管
TASK_APPROVAL_AGING_H = 24  # R87 待批过久提示：认领后仍未批准超过该时长，只提示复核，不是硬 SLA
# R38：WIP（在办任务）上限，防止一个 agent 囤一堆活却推进不了。0=不限；
# 环境变量 AGENTHUB_MAX_WIP 可覆盖（测试/临时放宽）。
try:
    MAX_WIP_PER_AGENT = max(0, int(os.environ.get("AGENTHUB_MAX_WIP", "3") or 3))
except (TypeError, ValueError):
    MAX_WIP_PER_AGENT = 3
# R42/R43：失败重试与死信。退避 = base * 2^(attempts-1)，封顶 1h；到上限转 dead。
try:
    MAX_TASK_ATTEMPTS = max(1, int(os.environ.get("AGENTHUB_MAX_ATTEMPTS", "3") or 3))
except (TypeError, ValueError):
    MAX_TASK_ATTEMPTS = 3
try:
    RETRY_BASE_SECONDS = max(1, min(int(os.environ.get("AGENTHUB_RETRY_BASE", "60") or 60), 3600))
except (TypeError, ValueError):
    RETRY_BASE_SECONDS = 60
# R26：在办任务「有推进」的容忍时长。租约/心跳只能证明人还在，证明不了任务在走；
# 超过该时长既没有检查点也没有任务更新，就标「进展停滞」（不自动释放，只点名）。
TASK_PROGRESS_STALL_HOURS = 6
# R30：待办饥饿——「现在就能开工」却长期没人认领的 pending 任务。等前置/显式阻塞的
# 任务是在等待，不是没人干；3 天覆盖一个正常交付周期，避免把刚建的任务当问题。
TASK_STARVE_DAYS = 3
DEFAULT_PRIORITY = 2  # M1（2026-10-12）：任务缺省优先级（0急 1高 2中 3低）
# R11 文件租约 TTL（分钟）：短锁防「忘了释放」把别人挡在门外；上限 8h 防长期占坑。
FILE_LEASE_TTL_MIN = 30
FILE_LEASE_TTL_FLOOR = 1
FILE_LEASE_TTL_CAP = 480
CRYSTAL_PREFIX = "结晶 #"  # M2：完成链结晶记录的标题前缀（幂等识别用）
# M10：前沿队列四段中文名（hub_next / GUI 工作台 / 心跳开工推送同源，防文案漂移）
TASK_GROUP_CN = {"ready": "可认领", "doing": "进行中", "takeover": "可接管",
                 "waiting": "等待依赖", "backoff": "退避中", "deferred": "已延后"}

# R36：agent 管控状态。draining 是「收尾」——不接新活，在办任务照常续；
# paused/offline 连认领也拦。控制状态由人决定，心跳不覆盖。
AGENT_CONTROL_STATES = ("active", "draining", "paused", "offline")
AGENT_CONTROL_CN = {"active": "正常", "draining": "收尾", "paused": "暂停", "offline": "离线"}
# R79：agent 角色。worker=日常读写（默认）；manager=可执行全局冻结/归档/隔离/
# 记忆整合/自动化开关等管理动作。管理通道（CLI/GUI/REST，非 MCP 会话）隐式 manager，
# 不落成 agent，也不占注册表。角色由管理通道显式授予，agent 不能自我提权。
AGENT_ROLES = ("worker", "manager")
AGENT_ROLE_CN = {"worker": "执行", "manager": "管理"}

EVENT_KINDS = ("progress", "note", "error", "handoff")


def _claim_cutoff() -> str:
    """认领 TTL 截止时刻（早于它的 claimed_at 视为超时）。"""
    return (datetime.datetime.now() - datetime.timedelta(hours=CLAIM_TTL_HOURS)) \
        .isoformat(timespec="seconds")


def _touch_project_activity(conn, project: str, date: str = "") -> None:
    """任务生命周期也刷新项目活跃（R20）：停滞项目的修复建议是「hub_task_add 写下下一步」，
    此前写任务既不更新 updated 也不改 status，照着建议做项目仍显示停滞。
    与 add_record 同一守卫：archived 项目不因新任务复活。"""
    name = (project or "").strip()
    if not name:
        return
    conn.execute(
        "UPDATE projects SET updated=?, status='active' WHERE name=? AND status!='archived'",
        ((date or _now())[:10], name))


def _verify_deliverables(root: str, project: str, deliverables: str, since: str) -> dict:
    """R34 交付物证据验证（只读，fail-open）：逐条核对任务声明的「完成时应存在的产物」。

    与 R28 的「结晶无完成证据提示」的分工：那条只说「任务期间没有任何痕迹」，
    本函数核对**声明过的产物本身**——存在吗？是本轮产出的吗？

    每条判定：
      ok           文件存在且 mtime 不旧于 since（任务创建时刻）。旧文件不算数——
                   Enjambre proof-of-delivery：a file older than the task does not count，
                   否则拿半年前的旧报告也能「证明完成」。
      stale        文件存在但早于 since——疑似拿旧产物冒充本轮交付。
      missing      声明了却不存在。
      unverifiable http(s) URL——本地不校验，诚实标注（不假装验证过）。
    路径解析：绝对路径 / ~ 原样；相对路径先试 <root>/<project>/，再试 <root>/。
    单条异常（含坏路径触发 ValueError）只把该条判 missing 并附原因，
    绝不抛错阻断完成——证据是提示，不是闸门（完成权在持有者）。
    """
    items = []
    for raw in re.split(r"[\n,，;；]+", str(deliverables or "")):
        d = raw.strip()
        if not d:
            continue
        rec = {"path": d[:200], "state": "missing", "note": ""}
        try:
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", d):
                rec["state"] = "unverifiable"
                rec["note"] = "URL 不本地校验"
                items.append(rec)
                continue
            p = Path(os.path.expandvars(os.path.expanduser(d)))
            if not p.is_absolute():
                cands = []
                if (project or "").strip():
                    cands.append(Path(root) / project.strip() / d)
                cands.append(Path(root) / d)
                p = next((c for c in cands if c.exists()), cands[0])
            if not p.exists():
                rec["note"] = "不存在"
            else:
                mt = datetime.datetime.fromtimestamp(p.stat().st_mtime)
                try:
                    since_dt = datetime.datetime.fromisoformat((since or "").strip())
                except (TypeError, ValueError):
                    since_dt = None
                if since_dt and mt < since_dt:
                    rec["state"] = "stale"
                    rec["note"] = f"文件时间 {mt:%Y-%m-%d %H:%M} 早于任务创建"
                else:
                    rec["state"] = "ok"
        except (OSError, ValueError) as e:
            rec["note"] = f"核对失败：{type(e).__name__}"
        items.append(rec)
    counts = {s: sum(1 for x in items if x["state"] == s)
              for s in ("ok", "stale", "missing", "unverifiable")}
    return {"items": items, "counts": counts, "total": len(items),
            "complete": bool(items) and counts["missing"] == 0 and counts["stale"] == 0}


def task_add(root: str, title: str, project: str = "", detail: str = "",
             blocked_by: str = "", agent: str = "", priority=2,
             deps: str = "", due: str = "", deliverables: str = "",
             skills: str = "", approval_required=False) -> tuple:
    """新建任务（P3-2 状态机入口；M1 加工作图字段）。
    priority：0急~3低（缺省 2）；deps：前置任务 id 逗号分隔（未完成前不会被 hub_next
    推荐、不可认领）；due：截止日（可空）。R3：deps 拒绝自依赖与环（含对「未来 id」的
    前向引用成环——插入后校验，成环则回滚该任务）；不存在的 id 仍允许（前向引用），
    由看板/巡检标「(不存在)」。R34：deliverables 声明「完成时应存在的产物」
    （换行/逗号分隔，路径相对项目目录或绝对路径，也接受 URL），done 时逐条核验。
    返回 (错误或 "", 任务 id)。"""
    title = (title or "").strip()
    if not title:
        return "title 必填", 0
    project = (project or "").strip()
    if project:
        perr = core.validate_project_ref(project)
        if perr:
            return perr + "（project 会参与目录定位与筛选，必须是普通名称）", 0
    try:
        pr = max(0, min(int(priority), 3))
    except (TypeError, ValueError):
        pr = DEFAULT_PRIORITY
    deps_raw = (deps or "").strip()
    dep_ids = _parse_task_deps(deps_raw)
    if deps_raw and not dep_ids:
        return f"deps 无合法任务 id：{deps_raw[:40]}（用逗号分隔任务编号）", 0
    deps_norm = ",".join(str(x) for x in dep_ids)[:200]
    with db_conn(root) as conn:
        cur = conn.execute(
            "INSERT INTO tasks(project,title,detail,status,blocked_by,priority,deps,due,"
            "created,updated,progress_at,created_by,deliverables,skills,approval_required) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project.strip(), title[:200], (detail or "").strip()[:2000],
             "blocked" if (blocked_by or "").strip() else "pending",
             (blocked_by or "").strip()[:200], pr, deps_norm, (due or "").strip()[:20],
             _now(), _now(), _now(), _norm_agent(agent) or "",
             (deliverables or "").strip()[:1000], _norm_skills(skills),
             1 if approval_required else 0))
        tid = cur.lastrowid
        if dep_ids:
            cyc = _dep_cycle_path(conn, tid, dep_ids)
            if cyc:
                conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
                return (f"依赖成环：{_dep_path_text(cyc)}（任务未创建；"
                        "前置关系必须是有向无环图）"), 0
        _touch_project_activity(conn, project)
        _journal_row(conn, _norm_agent(agent), "任务新建", f"tasks#{tid}",
                     f"{title[:60]}" + (f"｜deps={deps_norm}" if deps_norm else "")
                     + (f"｜project={project.strip()}" if project.strip() else ""))
        return "", tid


def task_list(root: str, status: str = "", owner: str = "", project: str = "",
              limit: int = 50) -> list:
    """任务列表（进行中 → 阻塞 → 待办 → 完成/已取消 排序，同级按 id 倒序）。"""
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


def task_count(root: str, status: str = "", owner: str = "", project: str = "") -> int:
    """任务总数（R271）：与 task_list 同过滤口径——列表有 LIMIT，
    抬头写「任务 N 条」不能是「取到的条数」。"""
    q, args = "SELECT COUNT(*) AS n FROM tasks WHERE 1=1", []
    if status:
        q += " AND status=?"
        args.append(status)
    if owner:
        q += " AND owner=?"
        args.append(_norm_agent(owner) or owner)
    if project:
        q += " AND project=?"
        args.append(project)
    with db_conn(root) as conn:
        return int(conn.execute(q, args).fetchone()["n"] or 0)


def task_approvals(root: str, project: str = "", limit: int = 50) -> list:
    """R86 待审批队列：在办、要求审批且尚未批准的任务，按认领时间升序（等最久的先看）。"""
    q = ("SELECT * FROM tasks WHERE status='in_progress' AND approval_required=1 "
         "AND COALESCE(approved_by,'')=''")
    args: list = []
    if project:
        q += " AND project=?"
        args.append(project)
    q += (" ORDER BY CASE WHEN COALESCE(claimed_at,'')='' THEN 1 ELSE 0 END, "
          "claimed_at ASC, id ASC LIMIT ?")
    args.append(max(1, min(int(limit or 50), 200)))
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(q, args)]


def task_approval_count(root: str, project: str = "") -> int:
    """待审批任务总数（R273）：队列有 LIMIT，抬头不能把取到的条数当总数。"""
    q = ("SELECT COUNT(*) FROM tasks WHERE status='in_progress' AND approval_required=1 "
         "AND COALESCE(approved_by,'')=''")
    args: list = []
    if project:
        q += " AND project=?"
        args.append(project)
    with db_conn(root) as conn:
        return int(conn.execute(q, args).fetchone()[0])


def task_get(root: str, tid) -> dict:
    """按 id 取任务行（不存在返回 {}）。R35：记录绑定/漂移核对共用的只读入口。"""
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return {}
    with db_conn(root) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else {}


def task_touch(root: str, tid, agent: str) -> tuple:
    """R35：持有者的真实动作（写工作记录等）→ 续租 + 推进 progress_at，不改变状态。
    仅当任务 in_progress 且 owner 正是本 agent 时生效；其余情况只返回行，由调用方
    提示——记录绑定 ≠ 认领，开工仍应走 hub_claim。返回 (错误或"", 任务行)。"""
    who = _norm_agent(agent) or ""
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return f"任务 id 非法：{tid!r}", {}
    if not who:
        return "agent 必填", {}
    now = _now()
    with db_conn(root) as conn:
        conn.execute(
            "UPDATE tasks SET claimed_at=?, updated=?, progress_at=? "
            "WHERE id=? AND status='in_progress' AND owner=?", (now, now, now, tid, who))
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return f"任务 #{tid} 不存在", {}
        return "", dict(row)


def _norm_skills(value) -> str:
    """R37：技能声明归一（逗号/中文逗号/分号/空白分隔，小写去重）。"""
    if isinstance(value, (list, tuple, set)):
        raw = ",".join(str(x) for x in value)
    else:
        raw = str(value or "")
    out: list = []
    for p in re.split(r"[,，;；\s]+", raw):
        p = p.strip().lower()[:40]
        if p and p not in out:
            out.append(p)
    return ",".join(out[:20])


def agent_skills_get(root: str, agent: str) -> list:
    key = _norm_agent(agent)
    if not key:
        return []
    try:
        with db_conn(root) as conn:
            row = conn.execute("SELECT skills FROM agents WHERE name=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return []
    return [s for s in _norm_skills(row["skills"] if row else "").split(",") if s]


def agent_skills_set(root: str, agent: str, skills, by: str = "") -> tuple:
    """R37：声明 agent 能力（覆盖式）。空串=清除声明（匹配时 fail-open）。"""
    key = _norm_agent(agent)
    if not key:
        return "agent 必填", []
    val = "" if str(skills or "").strip() == "-" else _norm_skills(skills)
    with db_conn(root) as conn:
        _upsert_agent(conn, key)
        conn.execute("UPDATE agents SET skills=? WHERE name=?", (val, key))
        _journal_row(conn, _norm_agent(by) or key, "agent 技能", key,
                     val or "清除声明")
    return "", [s for s in val.split(",") if s]


def _skill_match(task_skills: str, agent_skills: str) -> bool:
    """R37：任务要求与 agent 能力的交集判定。任一方未声明 → 放行（兼容存量，
    避免「没填技能就永远接不了活」）。"""
    req = {s for s in _norm_skills(task_skills).split(",") if s}
    have = {s for s in _norm_skills(agent_skills).split(",") if s}
    if not req or not have:
        return True
    return bool(req & have)


def agent_control_get(root: str, agent: str) -> dict:
    """R36：读 agent 管控状态（未登记/旧库缺列一律按 active，不猜停用）。"""
    key = _norm_agent(agent)
    if not key:
        return {"agent": "", "state": "active", "note": "", "at": ""}
    try:
        with db_conn(root) as conn:
            row = conn.execute(
                "SELECT control_state, control_note, control_at FROM agents WHERE name=?",
                (key,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    state = (dict(row).get("control_state") if row else "") or "active"
    if state not in AGENT_CONTROL_STATES:
        state = "active"
    return {"agent": key, "state": state,
            "note": (dict(row).get("control_note") or "") if row else "",
            "at": (dict(row).get("control_at") or "") if row else ""}


def agent_control_map(root: str) -> dict:
    """R36：全量 agent → 控制状态（列表/巡检一次读，避免逐人查询）。"""
    try:
        with db_conn(root) as conn:
            return {r["name"]: (r["control_state"] or "active")
                    for r in conn.execute("SELECT name, control_state FROM agents")}
    except sqlite3.OperationalError:
        return {}


def agent_role_get(root: str, agent: str) -> str:
    """R79：读 agent 角色。未登记/旧库缺列/非法值一律按 worker（最小权限）。"""
    key = _norm_agent(agent)
    if not key:
        return "worker"
    try:
        with db_conn(root) as conn:
            row = conn.execute("SELECT role FROM agents WHERE name=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return "worker"
    role = ((row["role"] if row else "") or "worker").strip().lower()
    return role if role in AGENT_ROLES else "worker"


def agent_role_map(root: str) -> dict:
    """R79：全量 agent → 角色（列表/巡检一次读）。缺列时全员 worker。"""
    try:
        with db_conn(root) as conn:
            return {r["name"]: ((r["role"] or "worker").strip().lower()
                                if (r["role"] or "").strip().lower() in AGENT_ROLES
                                else "worker")
                    for r in conn.execute("SELECT name, role FROM agents")}
    except sqlite3.OperationalError:
        return {}


def agent_role_set(root: str, agent: str, role: str, by: str = "") -> tuple:
    """R79：授予/收回角色。返回 (错误或 "", 角色行)。只有管理通道/manager 能调用。"""
    key = _norm_agent(agent)
    val = (role or "").strip().lower()
    if not key:
        return "agent 必填", {}
    if val not in AGENT_ROLES:
        return f"角色只能是 {'/'.join(AGENT_ROLES)}（收到 {role!r}）", {}
    now = _now()
    with db_conn(root) as conn:
        _upsert_agent(conn, key)
        conn.execute("UPDATE agents SET role=? WHERE name=?", (val, key))
        _journal_row(conn, _norm_agent(by) or "管理面", "agent 角色", key,
                     AGENT_ROLE_CN.get(val, val) + f"（{val}）")
    return "", {"agent": key, "role": val, "at": now}


def agent_control_set(root: str, agent: str, state: str, note: str = "",
                      by: str = "") -> tuple:
    """R36：切换 agent 管控状态。返回 (错误或 "", 状态行)。切换本身写 journal。"""
    key = _norm_agent(agent)
    st = (state or "").strip().lower()
    if not key:
        return "agent 必填", {}
    if st not in AGENT_CONTROL_STATES:
        return f"控制状态只能是 {'/'.join(AGENT_CONTROL_STATES)}（收到 {state!r}）", {}
    now = _now()
    with db_conn(root) as conn:
        _upsert_agent(conn, key)
        conn.execute(
            "UPDATE agents SET control_state=?, control_note=?, control_at=? WHERE name=?",
            (st, (note or "").strip()[:300], now, key))
        _journal_row(conn, _norm_agent(by) or key, "agent 管控", key,
                     f"{AGENT_CONTROL_CN.get(st, st)}"
                     + (f"｜{note[:80]}" if (note or "").strip() else ""))
    return "", agent_control_get(root, key) | {"at": now}


def _agent_control_gate(conn, agent: str) -> str:
    """R36：认领闸门。draining/paused/offline 都拒绝新认领；active 放行。"""
    key = _norm_agent(agent)
    if not key:
        return ""
    try:
        row = conn.execute("SELECT control_state, control_note FROM agents WHERE name=?",
                           (key,)).fetchone()
    except sqlite3.OperationalError:
        return ""
    if not row:
        return ""
    st = (row["control_state"] or "active").strip()
    if st not in AGENT_CONTROL_STATES or st == "active":
        return ""
    why = f"（{row['control_note'][:60]}）" if (row["control_note"] or "").strip() else ""
    tail = ("完成在办任务后切回 active 再接新活"
            if st == "draining" else "恢复 active 后再认领")
    return (f"认领失败：agent「{key}」当前为「{AGENT_CONTROL_CN.get(st, st)}」"
            f"{why}，不接新任务——{tail}。切换：hub_agent(action=\"control\", "
            f"agent=\"{key}\", state=\"active\")")


def _skill_gate(conn, row: dict, agent: str) -> str:
    """R37：任务声明技能时，已声明技能的 agent 必须至少命中一项；未声明则放行。"""
    req = (row.get("skills") or "").strip()
    if not req:
        return ""
    key = _norm_agent(agent)
    if not key:
        return ""
    try:
        arow = conn.execute("SELECT skills FROM agents WHERE name=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return ""
    have = (arow["skills"] if arow else "") or ""
    if _skill_match(req, have):
        return ""
    need = "、".join(s for s in _norm_skills(req).split(",") if s)
    got = "、".join(s for s in _norm_skills(have).split(",") if s) or "未声明"
    return (f"认领失败：任务需要技能「{need}」，agent「{key}」已声明「{got}」不匹配——"
            "换具备该技能的 agent，或先用 hub_agent(action=\"skills\", ...) 补声明")


def task_wip(root: str, agent: str) -> dict:
    """R38：读 agent 的 WIP（在办任务数/上限）。limit=0 表示不限。"""
    who = _norm_agent(agent)
    row = {"agent": who, "limit": MAX_WIP_PER_AGENT, "open": 0, "at_limit": False,
           "tasks": []}
    if not who:
        return row
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, project, claimed_at FROM tasks "
            "WHERE owner=? AND status='in_progress' ORDER BY id", (who,))]
    row["open"] = len(rows)
    row["tasks"] = rows
    row["at_limit"] = MAX_WIP_PER_AGENT > 0 and len(rows) >= MAX_WIP_PER_AGENT
    return row


def _wip_gate(conn, agent: str) -> str:
    """R38：WIP 上限闸门。已在办的任务续租不走这里（只拦新认领）。"""
    key = _norm_agent(agent)
    if not key or MAX_WIP_PER_AGENT <= 0:
        return ""
    n = conn.execute("SELECT COUNT(*) FROM tasks WHERE owner=? AND status='in_progress'",
                     (key,)).fetchone()[0]
    if n < MAX_WIP_PER_AGENT:
        return ""
    return (f"认领失败：agent「{key}」在办 {n} 单，已达 WIP 上限 {MAX_WIP_PER_AGENT}"
            "——先完成、退回或交办一单再领新活（管理面可设 AGENTHUB_MAX_WIP 调整）")


def task_handoff(root: str, tid, from_agent: str, to_agent: str,
                 note: str = "") -> tuple:
    """R39 任务交办：把在办任务连同上下文转给另一个 agent。
    - 持有者本人（或管理面 from=""）才能交办；目标须在册、active、技能匹配且未到 WIP 上限。
    - 交接前先由原持有者写一条检查点（交给对方「做到哪了」），成功后释放任务名下文件租约，
      并给对方发一条信号；状态保持 in_progress，进度时钟刷新。
    返回 (错误或 "", 任务行；含 _handoff/_leases_released)。"""
    who = _norm_agent(from_agent)
    to = _norm_agent(to_agent)
    if not to:
        return "to_agent 必填", {}
    if to == who:
        return "交办失败：目标就是当前持有者", {}
    key, aerr = check_agent(to, root)
    if aerr:
        return f"交办失败：{aerr}", {}
    to = key or to
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return f"任务 id 非法：{tid!r}", {}
    row = task_get(root, tid)
    if not row:
        return f"任务 #{tid} 不存在", {}
    if row["status"] != "in_progress":
        return (f"交办失败：任务 #{tid} 当前为「"
                f"{TASK_CN.get(row['status'], row['status'])}」，只有 in_progress 可交办"
                "（待办用 hub_claim 直接认领）"), row
    owner = (row["owner"] or "").strip()
    if who and owner != who:
        return f"交办失败：任务 #{tid} 由 {owner or '无人'} 持有，只有持有者可交办", row
    if not who and not owner:
        return f"交办失败：任务 #{tid} 无持有者，先 hub_claim 再交办", row
    # 先留上下文（写检查点必须在换主之前——R8 围栏会拒绝旧持有者的迟到写入）
    if who:
        try:
            checkpoint_save(root, tid, who,
                            f"交办给 {to}：{(note or '见任务详情').strip()[:500]}",
                            kind="manual")
        except Exception:  # noqa: BLE001  检查点失败不阻断交办（状态流转优先）
            pass
    with db_conn(root) as conn:
        crow = conn.execute("SELECT control_state FROM agents WHERE name=?", (to,)).fetchone()
        st = (crow["control_state"] if crow else "active") or "active"
        if st != "active":
            return (f"交办失败：目标 agent「{to}」当前为「{AGENT_CONTROL_CN.get(st, st)}」"
                    "，不接新任务"), row
        sgate = _skill_gate(conn, row, to)
        if sgate:
            return sgate.replace("认领失败：", "交办失败：", 1), row
        wgate = _wip_gate(conn, to)
        if wgate:
            return wgate.replace("认领失败：", "交办失败：", 1), row
        now = _now()
        sql = ("UPDATE tasks SET owner=?, claimed_at=?, updated=?, progress_at=? "
               "WHERE id=? AND status='in_progress'")
        args: list = [to, now, now, now, tid]
        if who:
            sql += " AND owner=?"
            args.append(who)
        cur = conn.execute(sql, args)
        if cur.rowcount != 1:
            return "交办失败：任务状态已变化（可能被接管），请刷新后重试", task_get(root, tid)
        released = _release_leases_for_task(conn, tid, who or "system", f"交办给 {to}")
        out = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        _journal_row(conn, who or "system", "交办任务", f"tasks#{tid}",
                     f"{owner or '-'}→{to}"
                     + (f"｜{note[:80]}" if (note or "").strip() else ""))
    try:
        signal_send(root, who or "system",
                    f"【交办】任务 #{tid}「{row['title'][:40]}」已转给你"
                    + (f"：{note[:120]}" if (note or "").strip() else "")
                    + "。用 hub_checkpoint 看上下文，hub_claim 续租开工。",
                    to_agent=to, project=row.get("project") or "")
    except Exception:  # noqa: BLE001  通知失败不影响交办结果
        pass
    out["_handoff"] = f"{owner or '管理面'}→{to}"
    out["_leases_released"] = released
    return "", out


def retry_wait_minutes(row: dict, now=None) -> int:
    """R42：任务剩余退避分钟（0=不在退避）。时间戳不可解析按 0 处理（fail-open）。"""
    stamp = (row.get("next_retry_at") or "").strip()
    if not stamp:
        return 0
    now = now or datetime.datetime.now()
    try:
        due = datetime.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return 0
    left = (due - now).total_seconds() / 60
    return max(0, int(math.ceil(left)))


def task_retry(root: str, tid, agent: str = "", reason: str = "",
               delay=None) -> tuple:
    """R42 失败重试：记下原因、放回待办，并按指数退避安排下次可认领时间。
    - reason 必填：没原因的失败无法复盘，也不该无限重试；
    - in_progress 只有持有者本人（或空 agent=管理面）可重试；
    - 第 MAX_TASK_ATTEMPTS 次失败自动进死信 dead（退出队列/WIP，并通知持有者）；
      dead 上再调用本函数即复活：次数清零重新排队（人工决定再给一次机会）；
    - delay 可显式给退避秒数（0=立即可认领），缺省 RETRY_BASE_SECONDS·2^(n-1)，上限 1h。
    返回 (错误或 "", 行；含 _attempts/_dead/_backoff_seconds/_retry_at)。"""
    who = _norm_agent(agent) or ""
    if not (reason or "").strip():
        return ("重试需写明 reason（失败原因/下次怎么改），没有依据的退避只会变成"
                "无限重试"), {}
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return f"任务 id 非法：{tid!r}", {}
    backoff = None
    if delay is not None and str(delay).strip() != "":
        try:
            backoff = max(0, min(int(delay), 86400))
        except (TypeError, ValueError):
            return "delay 必须是秒数（0=立即放回队列，上限 86400）", {}
    with db_conn(root) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return f"任务 #{tid} 不存在", {}
        row = dict(row)
        if row["status"] in ("done", "cancelled"):
            return (f"任务 #{tid} 已是「{TASK_CN.get(row['status'], row['status'])}」终态，"
                    "重试没有意义；需要重做请新建任务"), row
        owner = (row["owner"] or "").strip()
        if row["status"] == "in_progress" and owner and who and owner != who:
            return f"任务 #{tid} 由 {owner} 持有，只有持有者（或空传 agent）可重试", row
        revived = row["status"] == "dead"
        attempts = (0 if revived else int(row.get("attempts") or 0)) + 1
        if backoff is None:
            backoff = min(RETRY_BASE_SECONDS * (2 ** min(attempts - 1, 12)), 3600)
        dead = attempts >= MAX_TASK_ATTEMPTS
        now = _now()
        retry_at = ""
        if dead:
            sets = ("status='dead', attempts=?, last_error=?, next_retry_at='', "
                    "updated=?, progress_at=?")
            args: list = [attempts, reason.strip()[:500], now, now]
        else:
            retry_at = (datetime.datetime.now() + datetime.timedelta(seconds=backoff)
                        ).isoformat(timespec="seconds")
            sets = ("status='pending', owner='', claimed_at='', blocked_by='', "
                    "attempts=?, last_error=?, next_retry_at=?, updated=?, progress_at=?")
            args = [attempts, reason.strip()[:500], retry_at if backoff > 0 else "",
                    now, now]
        cur = conn.execute(f"UPDATE tasks SET {sets} WHERE id=? AND status=?",
                           (*args, tid, row["status"]))
        if cur.rowcount != 1:
            return "重试失败：任务状态已变化（可能被接管），请刷新后重试", task_get(root, tid)
        released = _release_leases_for_task(
            conn, tid, who or owner or "system", "任务死信" if dead else "任务重试")
        out = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        _touch_project_activity(conn, out["project"], now)
        _journal_row(conn, who or owner or "system",
                     "任务死信" if dead else "任务重试", f"tasks#{tid}",
                     f"第 {attempts} 次失败｜{reason.strip()[:120]}"
                     + ("" if dead else f"｜{backoff}s 后可认领"))
    if dead and owner:
        try:
            signal_send(root, who or "system",
                        f"【死信】任务 #{tid}「{out['title'][:40]}」重试 {attempts} 次仍失败，"
                        f"已退出队列：{reason.strip()[:120]}。处理后 hub_task_retry 复活。",
                        to_agent=owner, project=out.get("project") or "")
        except Exception:  # noqa: BLE001 通知失败不影响重试结果
            pass
    out["_attempts"] = attempts
    out["_dead"] = dead
    out["_backoff_seconds"] = backoff
    out["_retry_at"] = retry_at
    out["_leases_released"] = released
    return "", out


def defer_wait_hours(row: dict, now=None) -> float:
    """R44：任务剩余延后小时（0=未延后/已到点）。坏时间按 0 处理（fail-open）。"""
    stamp = (row.get("defer_until") or "").strip()
    if not stamp:
        return 0.0
    now = now or datetime.datetime.now()
    try:
        due = datetime.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return 0.0
    left = (due - now).total_seconds() / 3600
    return round(left, 1) if left > 0 else 0.0


def _parse_defer_until(value) -> tuple:
    """'YYYY-MM-DD' → 该日 00:00；ISO 时间原样；返回 (错误或"", iso)。"""
    raw = str(value or "").strip()
    if not raw:
        return "defer_until 必填（YYYY-MM-DD 或 ISO 时间）", ""
    stamp = raw + "T00:00:00" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else raw
    try:
        due = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return f"defer_until 需为 YYYY-MM-DD 或 ISO 时间，收到 {raw[:20]!r}", ""
    if due <= datetime.datetime.now():
        return "defer_until 必须在未来（现在就要做请直接认领）", ""
    return "", due.isoformat(timespec="seconds")


def task_defer(root: str, tid, until, reason: str = "", agent: str = "") -> tuple:
    """R44 任务延后（snooze）：暂不处理，退出前沿到 defer_until 再出现。
    与 retry 的分工：retry 是「失败后重试」，defer 是「现在不处理/等外部条件」——
    两者都不让任务凭空消失，但原因和恢复条件不同。
    - reason 必填；until 必须是未来日期/时间（日期按当天 00:00 恢复）；
    - in_progress 只有持有者（或空 agent=管理面）可延后；
    - 延后会释放 owner/租约/阻塞标记并清掉退避；终态不可延后。
    返回 (错误或 "", 行；含 _defer_hours/_defer_until)。"""
    who = _norm_agent(agent) or ""
    if not (reason or "").strip():
        return "延后需写明 reason（为什么现在不做/什么时候再看）", {}
    err, when = _parse_defer_until(until)
    if err:
        return err, {}
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return f"任务 id 非法：{tid!r}", {}
    with db_conn(root) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return f"任务 #{tid} 不存在", {}
        row = dict(row)
        if row["status"] in TASK_TERMINAL:
            return (f"任务 #{tid} 已是「{TASK_CN.get(row['status'], row['status'])}」终态，"
                    "不能延后"), row
        owner = (row["owner"] or "").strip()
        if row["status"] == "in_progress" and owner and who and owner != who:
            return f"任务 #{tid} 由 {owner} 持有，只有持有者（或空传 agent）可延后", row
        now = _now()
        cur = conn.execute(
            "UPDATE tasks SET status='pending', owner='', claimed_at='', blocked_by='', "
            "defer_until=?, defer_reason=?, deferred_at=?, next_retry_at='', "
            "updated=?, progress_at=? WHERE id=? AND status=?",
            (when, reason.strip()[:500], now, now, now, tid, row["status"]))
        if cur.rowcount != 1:
            return "延后失败：任务状态已变化（可能被接管），请刷新后重试", task_get(root, tid)
        released = _release_leases_for_task(conn, tid, who or owner or "system", "任务延后")
        out = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        _touch_project_activity(conn, out["project"], now)
        _journal_row(conn, who or owner or "system", "延后任务", f"tasks#{tid}",
                     f"至 {when}｜{reason.strip()[:100]}")
    out["_defer_hours"] = defer_wait_hours(out)
    out["_defer_until"] = when
    out["_leases_released"] = released
    return "", out


def task_undefer(root: str, tid, agent: str = "") -> tuple:
    """R44：清除延后标记（确认现在要做）。不改状态，只让任务重新可认领。"""
    who = _norm_agent(agent) or ""
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return f"任务 id 非法：{tid!r}", {}
    with db_conn(root) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return f"任务 #{tid} 不存在", {}
        row = dict(row)
        if row["status"] in TASK_TERMINAL:
            return f"任务 #{tid} 已是终态，不能恢复", row
        if not (row.get("defer_until") or "").strip():
            row["_was_deferred"] = False
            return "", row
        now = _now()
        conn.execute("UPDATE tasks SET defer_until='', defer_reason='', updated=?, "
                     "progress_at=? WHERE id=?", (now, now, tid))
        _journal_row(conn, who or "system", "恢复延后任务", f"tasks#{tid}",
                     f"原定 {(row.get('defer_until') or '')[:16]}")
        out = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        out["_was_deferred"] = True
    return "", out


def task_approve(root: str, tid, agent: str = "", note: str = "") -> tuple:
    """R45 完成审批：approval_required 的任务在 done 前必须由他人复核批准。
    four-eyes：署名 agent 不能是当前 owner；空 agent=管理面（owner 也空时可自管）。
    返回 (错误或 "", 行；含 _approved/_approved_by)。"""
    who = _norm_agent(agent) or ""
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return f"任务 id 非法：{tid!r}", {}
    with db_conn(root) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return f"任务 #{tid} 不存在", {}
        row = dict(row)
        if not int(row.get("approval_required") or 0):
            return (f"任务 #{tid} 未开启完成审批，不需要批准；直接 "
                    "hub_task_update(status=\"done\") 即可"), row
        if row["status"] in TASK_TERMINAL:
            return (f"任务 #{tid} 已是「{TASK_CN.get(row['status'], row['status'])}」终态，"
                    "审批没有意义"), row
        owner = (row["owner"] or "").strip()
        if who and owner and who == owner:
            return ("审批需他人复核：owner 不能给自己批准——换一个 agent，"
                    "或管理面空传 agent"), row
        now = _now()
        conn.execute("UPDATE tasks SET approved_by=?, approved_at=?, updated=?, "
                     "progress_at=? WHERE id=?",
                     (who or "管理面", now, now, now, tid))
        out = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        _journal_row(conn, who or "管理面", "任务审批", f"tasks#{tid}",
                     "批准完成" + (f"｜{note[:100]}" if (note or "").strip() else ""))
    if owner and who and who != owner:
        try:
            signal_send(root, who,
                        f"【审批通过】任务 #{tid}「{out['title'][:40]}」已由 {who} 批准完成"
                        + (f"：{note[:120]}" if (note or "").strip() else ""),
                        to_agent=owner, project=out.get("project") or "")
        except Exception:  # noqa: BLE001 通知失败不影响审批
            pass
    out["_approved"] = True
    out["_approved_by"] = who or "管理面"
    return "", out


def task_reject(root: str, tid, agent: str = "", reason: str = "") -> tuple:
    """R100 审批退回：复核不通过时把任务放回待办返工，而不是让 owner 无限空等。

    与批准同属 four-eyes：署名 agent 不能是当前 owner；原因必填（owner 要知道怎么改）。
    退回会释放 owner/租约/文件锁，任务回到 pending——修改后重新认领会再次进入审批队列；
    rejected_by/at/note 保留最近一次退回，reject_count 累计，供看板与审计追溯。
    已批准或非在办的任务不能退回（避免抹掉审批事实或凭空打回待办）。
    返回 (错误或 "", 行；含 _rejected_by/_reject_note)。
    """
    who = _norm_agent(agent) or ""
    reason = (reason or "").strip()
    if not reason:
        return "退回必须写明原因（owner 需要知道哪里不合格、怎么改）", {}
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return f"任务 id 非法：{tid!r}", {}
    with db_conn(root) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return f"任务 #{tid} 不存在", {}
        row = dict(row)
        if not int(row.get("approval_required") or 0):
            return (f"任务 #{tid} 未开启完成审批，不存在退回；若要打回重做，"
                    "用 hub_task_update(status=\"pending\", detail=...)"), row
        if row["status"] in TASK_TERMINAL:
            return (f"任务 #{tid} 已是「{TASK_CN.get(row['status'], row['status'])}」终态，"
                    "不能退回"), row
        if row["status"] != "in_progress":
            return (f"任务 #{tid} 当前是「{TASK_CN.get(row['status'], row['status'])}」，"
                    "不在审批队列（只有 in_progress 的待批任务可退回）"), row
        owner = (row["owner"] or "").strip()
        if who and owner and who == owner:
            return ("退回需他人复核：owner 不能退回自己的任务——换一个 agent，"
                    "或管理面空传 agent"), row
        approved_by = (row.get("approved_by") or "").strip()
        if approved_by:
            return (f"任务 #{tid} 已由 {approved_by} 批准完成，不能再退回；"
                    "如验收有误，请在 detail 记录问题并新建返工任务"), row
        now = _now()
        cur = conn.execute(
            "UPDATE tasks SET status='pending', owner='', claimed_at='', blocked_by='', "
            "approved_by='', approved_at='', rejected_by=?, rejected_at=?, reject_note=?, "
            "reject_count=COALESCE(reject_count,0)+1, next_retry_at='', defer_until='', "
            "defer_reason='', updated=?, progress_at=? "
            "WHERE id=? AND status='in_progress' AND approval_required=1 "
            "AND COALESCE(approved_by,'')=''",
            (who or "管理面", now, reason[:500], now, now, tid))
        if cur.rowcount != 1:
            return (f"任务 #{tid} 状态已变化（可能刚被批准或流转），退回未执行；"
                    "请刷新后重试"), row
        out = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        released = _release_leases_for_task(conn, tid, who or "管理面", "任务退回")
        _touch_project_activity(conn, out["project"], out["updated"])
        _journal_row(conn, who or "管理面", "任务退回", f"tasks#{tid}",
                     f"退回返工｜{reason[:400]}"
                     + (f"｜释放文件租约 {released} 个" if released else ""))
    if owner:
        try:
            signal_send(root, who or "管理面",
                        f"【审批退回】任务 #{tid}「{out['title'][:40]}」已由 "
                        f"{who or '管理面'} 退回返工：{reason[:200]}；"
                        "修改后重新 hub_claim，完成时仍需复核",
                        to_agent=owner, project=out.get("project") or "")
        except Exception:  # noqa: BLE001 通知失败不影响退回
            pass
    out["_rejected"] = True
    out["_rejected_by"] = who or "管理面"
    out["_reject_note"] = reason
    return "", out


def task_claim(root: str, tid, agent: str) -> tuple:
    """原子认领（P3-2）：pending → in_progress(owner)。M1：前置（deps）未全部完成时拒绝认领。
    并发安全：单条 UPDATE…WHERE（前置条件全写进 SQL）+ rowcount 判胜负（SQLite 写事务天然串行，
    两家同时 claim 只可能一家 rowcount=1）。超时自动释放：in_progress 且 claimed_at 早于 TTL
    的可被他人接管（防死锁）。R2：在办但无人可归（owner 空）或租约缺失（claimed_at 空）的
    任务同样可被认领——否则旧数据/异常退出留下的 in_progress 永远卡死，谁也接不了。
    返回 (错误或 "", 任务行 dict)；接管时行内含 _takeover 说明。"""
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
        renewed = bool(pre["status"] == "in_progress" and pre["owner"] == who)
        if not renewed:
            gate = _agent_control_gate(conn, who)
            if gate:
                return gate, pre
            sgate = _skill_gate(conn, pre, who)
            if sgate:
                return sgate, pre
            wgate = _wip_gate(conn, who)
            if wgate:
                return wgate, pre
        takeover = ""
        if pre["status"] == "in_progress" and pre["owner"] != who:
            if not (pre["owner"] or "").strip():
                takeover = "无主任务接管"
            elif not (pre["claimed_at"] or "").strip():
                takeover = "租约缺失接管"
            elif pre["claimed_at"] < cutoff:
                takeover = "租约过期接管"
        waiting = _task_deps_waiting(conn, pre)
        if waiting:
            names = "、".join(
                f"#{w['id']} {w['title'][:20]}（{TASK_CN.get(w['status'], w['status'])}）"
                for w in waiting[:4])
            more = " 等" if len(waiting) > 4 else ""
            return f"认领失败：任务 #{tid} 前置未完成——等待 {names}{more}", pre
        if pre["status"] == "pending" and (pre.get("blocked_by") or "").strip():
            return (f"认领失败：任务 #{tid} 仍有阻塞标记「{pre['blocked_by'][:40]}」；"
                    "确认解除后先 hub_task_update(blocked_by=\"-\") 清除"), pre
        defer_h = defer_wait_hours(pre)
        if pre["status"] == "pending" and defer_h > 0:
            return (f"认领失败：任务 #{tid} 延后中（约 {defer_h:g} 小时后恢复）"
                    f"（原因：{(pre.get('defer_reason') or '-')[:60]}）；"
                    "确认现在要做可 hub_task_update(defer_until=\"-\") 唤醒"), pre
        wait_min = retry_wait_minutes(pre)
        if pre["status"] == "pending" and wait_min > 0:
            return (f"认领失败：任务 #{tid} 退避中，还需 {wait_min} 分钟"
                    f"（第 {pre.get('attempts') or 0} 次失败："
                    f"{(pre.get('last_error') or '-')[:60]}）；到点再认领，"
                    "或由管理面 hub_task_retry(delay=0) 提前放行"), pre
        cur = conn.execute(
            "UPDATE tasks SET status='in_progress', owner=?, claimed_at=?, updated=?, "
            "progress_at=?, next_retry_at='', defer_until='', defer_reason='' "
            "WHERE id=? AND status<>'done' AND ("
            "  status='pending' "
            "  OR (status='in_progress' AND owner=?) "
            "  OR (status='in_progress' AND claimed_at<>'' AND claimed_at < ?) "
            "  OR (status='in_progress' AND (owner='' OR claimed_at=''))) "
            "AND (next_retry_at='' OR next_retry_at<=?) "
            "AND (defer_until='' OR defer_until<=?)",
            (who, now, now, now, tid, who, cutoff, now, now))
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return f"任务 #{tid} 不存在", {}
        row = dict(row)
        if cur.rowcount == 1:
            _touch_project_activity(conn, row["project"], now)
            # 同一 owner 再次认领 = 续租，不是新认领；调用方据此给用户准确提示。
            row["_renewed"] = renewed
            if takeover:
                row["_takeover"] = takeover
                # R11：接管后旧持有者的文件租约立即失效，否则新主人一开工就撞自己的锁。
                _release_leases_for_task(conn, tid, who, takeover)
            _journal_row(
                conn, who,
                "续租任务" if renewed else (f"接管任务（{takeover}）" if takeover else "认领任务"),
                f"tasks#{tid}", f"owner={who}｜{row['title'][:60]}")
            # 认领成功：M4 开工资锚点移到 conn 事务外补写（见下方）
        elif row["status"] == "done":
            return f"认领失败：任务 #{tid} 已完成（{row['done_at'][:10]}）", row
        elif row["status"] == "in_progress" and row["owner"] != who:
            return (f"认领失败：任务 #{tid} 已被 {row['owner']} 认领"
                    f"（{(row['claimed_at'] or '租约时间缺失')[:16]}；"
                    f"超 {CLAIM_TTL_HOURS}h 未更新可自动接管）"), row
        elif row["status"] == "blocked":
            return (f"认领失败：任务 #{tid} 处于阻塞（等待 {row['blocked_by'] or '未知'}）；"
                    "解除后用 hub_task_update status=pending"), row
        elif row["status"] == "dead":
            return (f"认领失败：任务 #{tid} 已进入死信（重试 {row.get('attempts') or 0} 次"
                    "仍失败）；先用 hub_task_retry 复活（会重置次数）"), row
        elif row["status"] == "cancelled":
            return (f"认领失败：任务 #{tid} 已取消（终态）"
                    + (f"：{(row.get('cancel_reason') or '')[:60]}"
                       if row.get("cancel_reason") else "")), row
        elif row["status"] == "pending" and defer_wait_hours(row) > 0:
            return (f"认领失败：任务 #{tid} 延后中（约 "
                    f"{defer_wait_hours(row):g} 小时后恢复）"), row
        elif row["status"] == "pending" and retry_wait_minutes(row) > 0:
            return (f"认领失败：任务 #{tid} 正在退避（还剩 "
                    f"{retry_wait_minutes(row)} 分钟）"), row
        else:
            return f"认领失败：任务 #{tid} 当前状态 {row['status']}", row
    # M4：认领成功自动存「开工资锚点」——断点续跑的第一块砖（崩溃/换人/隔天续跑时，
    # 检查点时间线的起点）。必须在 conn 事务**外**写：嵌套连接会被写锁挡（busy_timeout
    # 后 OperationalError，被 except 吞了就静默丢失——2026-10-12 实测踩坑）。
    # 失败只降级，不影响认领结果。
    if not row.get("_renewed"):
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
                deps: str = "", due: str = "", deliverables: str = "",
                skills: str = "", cancel_reason: str = "",
                approval_required=None) -> tuple:
    """任务状态流转（P3-2 + M1 + R41）：pending/in_progress/blocked/done/cancelled。
    持有者约束：in_progress 任务只有 owner 本人（或空 agent=管理面）可流转；
    →pending 释放并清 owner/claimed_at/blocked_by（否则残留阻塞标记会让任务永远进不了前沿）；
    →in_progress 必须署名，且与 hub_claim 同等地校验前置依赖——不能绕过认领闸门；
    blocked 必须先 →pending 解除，不能直接跳 in_progress；blocked_by 传 "-" 可单独清除。
    →done 记 done_at 并**自动结晶**（M2：独立事务，失败只降级为
    提示、不影响状态流转，结果附在返回行的 crystal 键）；done 后不可再改。
    R41 →cancelled：终态，必须写 cancel_reason；释放文件租约、退出所有队列与 WIP，
    保留 owner 供追溯（cancelled_by 记操作者）。
    M1 新字段：priority(0~3) / deps(前置 id，传 "-" 清除) / due(截止日，传 "-" 清除)；
    detail 同样可传 "-" 清除。
    R3：deps 拒绝自依赖与环（前向引用允许，成环拒绝），非法 id 串直接报错不静默清空。
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
        if row["status"] == "cancelled" and status and status != "cancelled":
            return (f"任务 #{tid} 已取消（终态），不能改回 {status}；"
                    "需要继续请新建任务，避免用复活抹掉取消原因"), row
        if row["status"] == "dead" and status and status != "dead":
            return (f"任务 #{tid} 已进入死信（重试耗尽），先 hub_task_retry 复活"
                    "（会重置次数并重新排队）"), row
        if status == "dead":
            return ("死信由 hub_task_retry 在重试耗尽时自动产生，不能手工设置；"
                    "失败请用 hub_task_retry 记录原因并退避"), row
        if (status == "done" and int(row.get("approval_required") or 0)
                and not (row.get("approved_by") or "").strip()):
            return (f"任务 #{tid} 要求完成审批：先由他人/管理面 "
                    "hub_task_approve(task_id=...) 批准，再标 done——"
                    "把「完成」从自证变成复核"), row
        if status == "cancelled" and not (cancel_reason or "").strip():
            return ("取消任务需写明 cancel_reason（为什么取消/替代方案），"
                    "避免任务凭空消失、复盘时无据可查"), row
        if (row["status"] == "in_progress" and row["owner"] and who
                and row["owner"] != who):
            return f"任务 #{tid} 由 {row['owner']} 持有，只有持有者（或空传 agent）可流转", row
        blocked_raw = (blocked_by or "").strip()
        if status == "in_progress":
            if row["status"] != "in_progress" and who:
                gate = _agent_control_gate(conn, who)
                if gate:
                    return gate, row
                sgate = _skill_gate(conn, row, who)
                if sgate:
                    return sgate, row
                wgate = _wip_gate(conn, who)
                if wgate:
                    return wgate, row
            if not who and not (row["owner"] or "").strip():
                return f"任务 #{tid} 转 in_progress 必须署名（用 hub_claim 或 agent=...）", row
            if row["status"] == "blocked":
                return (f"任务 #{tid} 处于阻塞（{row['blocked_by'] or '未写原因'}）；"
                        "先用 status=pending 解除，再认领"), row
            if row["status"] == "pending":
                defer_h = defer_wait_hours(row)
                if defer_h > 0:
                    return (f"任务 #{tid} 延后中（约 {defer_h:g} 小时后恢复，"
                            f"原因：{(row.get('defer_reason') or '-')[:60]}）；"
                            "现在要做先 hub_task_update(defer_until=\"-\") 唤醒"), row
                wait_min = retry_wait_minutes(row)
                if wait_min > 0:
                    return (f"任务 #{tid} 退避中（还剩 {wait_min} 分钟，"
                            f"第 {row.get('attempts') or 0} 次失败）；到点再开工，"
                            "或先 hub_task_retry(delay=0) 提前放行"), row
                waiting = _task_deps_waiting(conn, row)
                if waiting:
                    names = "、".join(
                        f"#{w['id']} {w['title'][:20]}（{TASK_CN.get(w['status'], w['status'])}）"
                        for w in waiting[:4])
                    more = " 等" if len(waiting) > 4 else ""
                    return f"任务 #{tid} 前置未完成——等待 {names}{more}", row
            if (row["owner"] or "").strip() and who and row["owner"] != who:
                return f"任务 #{tid} 由 {row['owner']} 持有，只有持有者（或空传 agent）可流转", row
        if status == "blocked" and not (blocked_raw or (row["blocked_by"] or "").strip()):
            return f"任务 #{tid} 转 blocked 需写明 blocked_by", row
        became_done = bool(status == "done" and row["status"] != "done")
        sets: list = []
        args: list = []
        if status:
            sets.append("status=?")
            args.append(status)
            if status == "done":
                sets.append("done_at=?")
                args.append(_now())
                sets.append("next_retry_at=''")
                sets.append("defer_until=''")
                sets.append("defer_reason=''")
            elif status == "cancelled":
                sets += ["cancelled_at=?", "cancel_reason=?", "cancelled_by=?"]
                args += [_now(), (cancel_reason or "").strip()[:500], who or "管理面"]
                sets.append("next_retry_at=''")
                sets.append("defer_until=''")
                sets.append("defer_reason=''")
            elif status == "pending":
                # 释放必须把阻塞标记一起清掉：pending + blocked_by 的组合会被前沿队列
                # 当成「等待中」而永远不推荐，也会让认领多一道无谓拦截。
                sets.append("owner=''")
                sets.append("claimed_at=''")
                sets.append("blocked_by=''")
                if row["status"] != "pending":
                    # 人工/流程把任务放回待办 = 明确要求尽快重新开工；
                    # pending→pending 只改字段时保留原有退避/延后，避免顺手抹掉。
                    sets.append("next_retry_at=''")
                    sets.append("defer_until=''")
                    sets.append("defer_reason=''")
            elif status == "in_progress":
                if who:
                    sets.append("owner=?")
                    args.append(who)
                sets.append("claimed_at=?")
                args.append(_now())
                sets.append("blocked_by=''")
                sets.append("next_retry_at=''")
                sets.append("defer_until=''")
                sets.append("defer_reason=''")
        if blocked_raw and status not in ("pending", "done", "in_progress"):
            if blocked_raw == "-":
                sets.append("blocked_by=''")
            else:
                sets.append("blocked_by=?")
                args.append(blocked_raw[:200])
        dt = (detail or "").strip()
        if dt:
            # 传 "-" 清除：与 deps/due/skills 同一约定，避免旧说明改不掉。
            if dt == "-":
                sets.append("detail=''")
            else:
                sets.append("detail=?")
                args.append(dt[:2000])
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
                dep_ids = _parse_task_deps(ds)
                if not dep_ids:
                    return f"deps 无合法任务 id：{ds[:40]}（用逗号分隔任务编号）", row
                cyc = _dep_cycle_path(conn, tid, dep_ids)
                if cyc:
                    return (f"依赖成环：{_dep_path_text(cyc)}（未更新；"
                            "前置关系必须是有向无环图）"), row
                sets.append("deps=?")
                args.append(",".join(str(x) for x in dep_ids)[:200])
        du = (due or "").strip()
        if du:
            if du == "-":
                sets.append("due=''")
            else:
                sets.append("due=?")
                args.append(du[:20])
        # R34 交付物声明：可随时改；done 时按最终值核验（见下方 evidence）。
        dv = (deliverables or "").strip()
        if dv:
            if dv == "-":
                sets.append("deliverables=''")
            else:
                sets.append("deliverables=?")
                args.append(dv[:1000])
        sv = (skills or "").strip()
        if sv:
            sets.append("skills=?")
            args.append("" if sv == "-" else _norm_skills(sv))
        # R45 完成审批开关：关闭时清掉旧批准，避免下次开启后沿用上一次的批准。
        if approval_required is not None and str(approval_required).strip() != "":
            want = 1 if str(approval_required).strip().lower() in (
                "1", "true", "yes", "on") else 0
            sets.append("approval_required=?")
            args.append(want)
            if not want:
                sets.append("approved_by=''")
                sets.append("approved_at=''")
        # 持有者在更新进展时顺手续租：任务被持续编辑说明人还在干，
        # 不应因为忘了重新 hub_claim 就被 2h TTL 判死。
        # R107：仅**非终态**流转续租——done/cancelled 是历史事实的封口，认领时刻
        # 不能被关单动作重写（实测 #13：owner 标 done 后 claimed_at=done_at=updated
        # 三时间戳同秒，任务时长归零、task_quality 报「0 秒即完成」、审批等待时长
        # 也被抹平）。终态后租约无意义，claimed_at 保留原认领时刻供时长统计。
        if (row["status"] == "in_progress" and row["owner"] and row["owner"] == who
                and status not in ("done", "cancelled")
                and not any(s.startswith("claimed_at") for s in sets)):
            sets.append("claimed_at=?")
            args.append(_now())
        if not sets:
            return ("没有要更新的字段（status/blocked_by/detail/priority/deps/due "
                    "至少传一个）", row)
        now_u = _now()
        sets.append("updated=?")
        args.append(now_u)
        # R26：真实任务动作才推进 progress_at；心跳续租不经过这里，故不计入推进。
        sets.append("progress_at=?")
        args.append(now_u)
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*args, tid))
        out = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        _touch_project_activity(conn, out["project"], out["updated"])
        changes = []
        if out["status"] != row["status"]:
            changes.append(f"状态 {TASK_CN.get(row['status'], row['status'])}→"
                           f"{TASK_CN.get(out['status'], out['status'])}")
        if out["status"] == "cancelled" and out.get("cancel_reason"):
            changes.append(f"取消原因 {out['cancel_reason'][:40]}")
        if out["owner"] != row["owner"]:
            changes.append(f"owner {row['owner'] or '无'}→{out['owner'] or '无'}")
        if (out["blocked_by"] or "") != (row["blocked_by"] or ""):
            changes.append("blocked_by " + ("清除" if not out["blocked_by"]
                                            else out["blocked_by"][:40]))
        if out["deps"] != row["deps"]:
            changes.append("deps " + ("清除" if not out["deps"] else out["deps"][:40]))
        if out["due"] != row["due"]:
            changes.append("due " + ("清除" if not out["due"] else out["due"][:20]))
        if out["priority"] != row["priority"]:
            changes.append(f"priority {row['priority']}→{out['priority']}")
        if int(out.get("approval_required") or 0) != int(row.get("approval_required") or 0):
            changes.append("完成审批 " + ("开启" if int(out["approval_required"] or 0)
                                          else "关闭"))
        if (out["detail"] or "") != (row["detail"] or ""):
            changes.append("detail 更新")
        if out["claimed_at"] != row["claimed_at"] and out["status"] == "in_progress":
            changes.append("租约更新")
        # R11：任务离开在办（done/pending/blocked）或换人时，挂在其上的文件租约一并释放。
        if row["status"] == "in_progress" and (
                out["status"] != "in_progress"
                or (out["owner"] or "") != (row["owner"] or "")):
            _release_leases_for_task(
                conn, tid, who or out["owner"] or "user",
                "任务" + TASK_CN.get(out["status"], out["status"]))
        _journal_row(conn, who or out["owner"] or "user", "任务流转", f"tasks#{tid}",
                     "｜".join(changes) or "更新（字段值未变）")
    if became_done:
        # M2 完成链自动结晶：独立事务，失败只降级为提示、不影响状态流转
        try:
            cerr, csum = task_crystallize(root, tid)
            out["crystal"] = csum if not cerr else f"未结晶（{cerr}）"
        except Exception as e:  # noqa: BLE001
            out["crystal"] = f"结晶异常（{type(e).__name__}: {str(e)[:80]}）"
        # R34 交付物证据：done 时对声明的产物逐条核验（只读、fail-open）。
        # 与结晶的「无完成证据」提示互补——那条说「没有任何痕迹」，这条说「声明过的
        # 产物在不在、是不是本轮的」。结论附 evidence 键，阻断权永远在持有者。
        try:
            out["evidence"] = _verify_deliverables(root, out.get("project", ""),
                                                   out.get("deliverables", ""),
                                                   out.get("created", ""))
        except Exception as e:  # noqa: BLE001  核验失败不拖垮完成本身
            out["evidence"] = {"error": f"{type(e).__name__}: {str(e)[:80]}",
                               "total": 0, "complete": False, "counts": {}, "items": []}
        # R40 完成质量体检：建议性质，附在完成结果里；异常同样不拖垮状态流转。
        try:
            qerr, qrep = task_closure_quality(root, tid)
            out["quality"] = qrep if not qerr else {"error": qerr}
        except Exception as e:  # noqa: BLE001
            out["quality"] = {"error": f"{type(e).__name__}: {str(e)[:80]}"}
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


def _dep_cycle_path(conn, tid: int, deps: list):
    """R3：把 tid 的出边临时设为 deps，检测是否存在经过 tid 的依赖环。
    有环返回 [tid, …, tid] 路径，无环返回 None（BFS，避免深链递归爆栈）。"""
    graph = {r["id"]: _parse_task_deps(r["deps"]) for r in conn.execute(
        "SELECT id, deps FROM tasks")}
    graph[tid] = list(deps)
    parent = {tid: None}
    queue = [tid]
    head = 0
    while head < len(queue):
        cur = queue[head]
        head += 1
        for nxt in graph.get(cur, []):
            if nxt == tid:
                path = []
                x = cur
                while x is not None:
                    path.append(x)
                    x = parent[x]
                path.reverse()
                path.append(tid)
                return path
            if nxt not in parent:
                parent[nxt] = cur
                queue.append(nxt)
    return None


def _dep_path_text(path: list) -> str:
    return " → ".join(f"#{x}" for x in path)


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


def task_graph_audit(root: str) -> dict:
    """R4 任务图结构审计（只读）：前置缺失 / 依赖成环 / pending 残留阻塞标记。
    R3 已在写入口拒绝成环；这里兜存量数据与绕过接口直改库的情况，供巡检与看板共用。"""
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,project,title,status,owner,blocked_by,deps FROM tasks")]
    ids = {r["id"] for r in rows}
    graph = {r["id"]: _parse_task_deps(r["deps"]) for r in rows}
    dangling, residual = [], []
    for r in rows:
        missing = [d for d in graph[r["id"]] if d not in ids]
        if missing:
            r["_missing"] = missing
            dangling.append(r)
        if r["status"] == "pending" and (r["blocked_by"] or "").strip():
            residual.append(r)
    cycles, seen = [], set()
    state: dict = {}
    for start in sorted(ids):
        if state.get(start, 0):
            continue
        state[start] = 1
        stack = [(start, iter(graph.get(start, [])))]
        path = [start]
        while stack:
            node, it = stack[-1]
            try:
                nxt = next(it)
            except StopIteration:
                state[node] = 2
                stack.pop()
                path.pop()
                continue
            if nxt not in ids:
                continue
            st = state.get(nxt, 0)
            if st == 0:
                state[nxt] = 1
                stack.append((nxt, iter(graph.get(nxt, []))))
                path.append(nxt)
            elif st == 1:
                cyc = path[path.index(nxt):] + [nxt]
                key = frozenset(cyc)
                if key not in seen:
                    seen.add(key)
                    cycles.append(cyc)
    cycles.sort(key=lambda c: min(c))
    return {"dangling": dangling, "cycles": cycles, "residual_blocked": residual,
            "counts": {"dangling": len(dangling), "cycles": len(cycles),
                       "residual_blocked": len(residual)}}


def task_next(root: str, project: str = "", owner: str = "", limit: int = 5) -> dict:
    """M1 前沿队列（Frontier）：依赖已满足、可直接开工的待办推荐；附进行中/可接管/等待。
    R2：可接管段同时收「租约过期」「无主」「租约缺失」三类（附 _takeover 原因）。
    排序：priority（0 急 → 3 低）→ id（先来先做）。纯只读。返回结构化 dict 供 MCP 层排版。
    R727：`hidden`（{"control": n, "skills": n}）记录被管控状态/能力声明挡掉的条数——
    counts 在过滤之后才算，调用方需要它才能区分「前沿真的空」与「活有但接不住」。
    R803：limit 上限放宽到 200——GUI 需要「显示更多」逐段展开；MCP 默认仍是 5。
    R806：waiting 段带 `_wait_kind`（deps=等前置任务 / blocked=被外部挡住），
    counts 同步给 `waiting_deps`/`waiting_blocked`——两者混报成「等待依赖」会误导。"""
    try:
        lim = max(1, min(int(limit or 5), 200))
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
        ready, doing, takeover, waiting, backoff, deferred = [], [], [], [], [], []
        # R806：waiting 段混着两种完全不同的「在等」，计数要分开，摘要才说得准。
        n_wait_deps = n_wait_blocked = 0
        for r in rows:
            try:
                r["_priority"] = int(r.get("priority"))
            except (TypeError, ValueError):
                r["_priority"] = DEFAULT_PRIORITY
            if r["status"] == "in_progress":
                owner_s = (r["owner"] or "").strip()
                lease_s = (r["claimed_at"] or "").strip()
                expired = bool(lease_s) and lease_s < cutoff
                # R2：无主/无租约的在办任务是「可接管」而不是「进行中」——
                # 否则看板会把一条谁也认领不了（旧代码）或没人负责的活当成有人在干。
                unleased = (not owner_s) or (not lease_s)
                if unleased and (not owner_s or owner_s != me):
                    r["_takeover"] = "无主" if not owner_s else "租约缺失"
                    takeover.append(r)
                elif expired and owner_s != me:
                    r["_takeover"] = "租约过期"
                    takeover.append(r)
                elif (not me) or owner_s == me:
                    doing.append(r)
                continue
            if r["status"] == "pending":
                wait_h = defer_wait_hours(r)
                if wait_h > 0:
                    # R44：延后中的任务单独成段——不是没活干，是主动排到以后。
                    r["_defer_wait_h"] = wait_h
                    deferred.append(r)
                    continue
                wait_min = retry_wait_minutes(r)
                if wait_min > 0:
                    # R42：退避中的任务单独成段——不是没活干，是刚失败、还没到点。
                    r["_retry_wait_min"] = wait_min
                    backoff.append(r)
                    continue
            wait = _task_deps_waiting(conn, r)
            if r["status"] == "blocked" or wait or (r["blocked_by"] or "").strip():
                r["_wait"] = wait
                # R806：等前置任务（deps 里还有没做完的 id）与被外部挡住是两件事——
                # 后者 blocked_by 写了人能看懂的原因（用户决策/需要真机复测），旧实现
                # 一律叫「等待依赖」，真实库 #6/#8 deps 为空时页头报「等待依赖 2」且
                # 原因一个字都不露，等于把「等谁」这条唯一有用的信息丢了。
                r["_wait_kind"] = "deps" if wait else "blocked"
                waiting.append(r)
                if wait:
                    n_wait_deps += 1
                else:
                    n_wait_blocked += 1
            else:
                ready.append(r)
    ctrl = agent_control_get(root, me) if me else {"agent": "", "state": "active", "note": ""}
    hidden = {"control": 0, "skills": 0, "skills_wanted": []}
    if me and ctrl["state"] in ("draining", "paused", "offline"):
        # 收尾/暂停的 agent 不再被推荐新活；在办与等待段仍可见，便于收口。
        # R727：藏了几条要如实报出去——调用方分不清「库里没活」和「活被状态挡住」。
        hidden["control"] = len(ready) + len(takeover)
        ready, takeover = [], []
    have_skills = agent_skills_get(root, me) if me else []
    if have_skills:
        # R37：声明了能力的 agent 只看得到自己接得住的任务；没声明则不筛（兼容存量）。
        have_s = ",".join(have_skills)
        dropped = [t for t in ready + takeover
                   if not _skill_match(t.get("skills") or "", have_s)]
        ready = [t for t in ready if _skill_match(t.get("skills") or "", have_s)]
        takeover = [t for t in takeover if _skill_match(t.get("skills") or "", have_s)]
        hidden["skills"] = len(dropped)
        # 被挡的活要什么技能一并给出：只说「筛掉了 3 条」等于让人再猜一轮
        hidden["skills_wanted"] = sorted({s.strip() for t in dropped
                                          for s in (t.get("skills") or "").split(",")
                                          if s.strip()})
    ready.sort(key=lambda t: (t["_priority"], t["id"]))
    doing.sort(key=lambda t: (t["_priority"], t["id"]))
    waiting.sort(key=lambda t: (t["_priority"], t["id"]))
    backoff.sort(key=lambda t: (t.get("_retry_wait_min") or 0, t["_priority"], t["id"]))
    deferred.sort(key=lambda t: (t.get("_defer_wait_h") or 0, t["_priority"], t["id"]))
    return {"ready": ready[:lim], "doing": doing[:lim], "takeover": takeover[:lim],
            "waiting": waiting[:lim], "backoff": backoff[:lim],
            "deferred": deferred[:lim], "control": ctrl,
            "hidden": hidden, "skills": list(have_skills),
            "counts": {"ready": len(ready), "doing": len(doing),
                       "takeover": len(takeover), "waiting": len(waiting),
                       "backoff": len(backoff), "deferred": len(deferred),
                       # R806：waiting 按成因拆分，供摘要/段标题如实分说
                       "waiting_deps": n_wait_deps,
                       "waiting_blocked": n_wait_blocked}}


def _age_hours(stamp: str, now: datetime.datetime):
    """时间戳 → 距今小时数（一位小数）；空/坏时间返回 None，不猜。"""
    if not (stamp or "").strip():
        return None
    try:
        return round((now - datetime.datetime.fromisoformat(stamp)).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


def _task_progress(row: dict, last_cp: str, now: datetime.datetime) -> tuple:
    """在办任务的最后推进 → (距今小时数, 是否停滞)。

    推进信号取 tasks.progress_at 与最新**手工**检查点的较新者：认领、状态流转、改详情、
    写检查点都会写 progress_at，检查点单独取一次防「只存进展、任务行没动」。不能用
    tasks.updated——心跳续租也会刷它，等于把「人在线」冒充成「任务在走」，停滞永远不可见。
    老行无 progress_at（迁移前由旧进程写入）时回落 updated，宁可晚发现也不误报。
    非在办任务返回 (None, False)；无推进证据返回 (None, True)。
    """
    if row.get("status") != "in_progress":
        return None, False
    own = (row.get("progress_at") or "").strip() or (row.get("updated") or "").strip()
    last = max(own, (last_cp or "").strip())
    if not last:
        return None, True
    age = _age_hours(last, now)
    if age is None:
        return None, True
    return age, age >= TASK_PROGRESS_STALL_HOURS


def _checkpoint_index(conn) -> dict:
    """任务 → 最新手工检查点时间（一次批量查询，供看板/巡检共用）。

    认领自动锚点（auto_claim）是系统写的起点标记，不是推进证据；认领本身已写
    progress_at，计入锚点会让「领了就停」的任务多骗一个停滞窗口。
    """
    return {r["task_id"]: r["ts"] for r in conn.execute(
        "SELECT task_id, MAX(ts) AS ts FROM checkpoints "
        "WHERE kind<>'auto_claim' GROUP BY task_id")}


def _sessions_index(conn) -> dict:
    """agent → 最近心跳时间（同一 agent 多项目会话取最新；一次批量查询）。"""
    return {r["agent"]: r["ts"] for r in conn.execute(
        "SELECT agent, MAX(ts) AS ts FROM sessions GROUP BY agent")}


def _task_clock_fields(row: dict, cps: dict, sessions: dict,
                       now: datetime.datetime) -> dict:
    """R27 任务时间口径：推进/检查点/心跳分开，谁都不能冒充谁。

    心跳是 **agent 级** 的存活信号（owner 的最近心跳），不是任务级进度；
    检查点是任务级断点，但认领自动锚点不算推进；推进以 progress_at 为准。
    """
    cp = (cps or {}).get(row.get("id"), "") or ""
    hb = (sessions or {}).get(_norm_agent(row.get("owner") or "") or "", "") or ""
    age, stalled = _task_progress(row, cp, now)
    return {"_last_cp": cp,
            "_last_cp_age_h": _age_hours(cp, now),
            "_heartbeat_at": hb,
            "_heartbeat_age_h": _age_hours(hb, now),
            "_progress_age_h": age,
            "_progress_stall": stalled}


def _task_lease(row: dict, cutoff: str, now: datetime.datetime) -> tuple:
    """在办任务租约状态 → (state, 剩余小时)；非在办返回 ("", None)。

    口径与 task_claim/task_next 一致：无主/租约缺失可接管，claimed_at 早于 cutoff 过期。
    """
    if row.get("status") != "in_progress":
        return "", None
    if not (row.get("owner") or "").strip():
        return "ownerless", None
    lease = (row.get("claimed_at") or "").strip()
    if not lease:
        return "unleased", None
    left = None
    try:
        left = round(CLAIM_TTL_HOURS
                     - (now - datetime.datetime.fromisoformat(lease)).total_seconds() / 3600, 1)
    except ValueError:
        pass
    return ("expired" if lease < cutoff else "ok"), left


def task_holdings(root: str) -> dict:
    """在办任务按持有者归组（R7 控制面）：谁手里压着活、租约是否还有效。

    一次只读查询供 hub_list_agents / hub_agent status 共用；空 owner 的行归到 "" 键
    （无主在办不隐藏）。每行附 _lease_state ∈ ok/unleased/expired/ownerless 与
    _lease_left_h（剩余小时，租约时间不可解析时为 None）。
    """
    now = datetime.datetime.now()
    cutoff = _claim_cutoff()
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, project, title, status, owner, claimed_at, due, priority, updated, "
            "progress_at FROM tasks WHERE status='in_progress' ORDER BY id")]
        cps = _checkpoint_index(conn)
        sessions = _sessions_index(conn)
    groups: dict = {}
    for r in rows:
        state, left = _task_lease(r, cutoff, now)
        r["_lease_state"] = state
        r["_lease_left_h"] = left
        r.update(_task_clock_fields(r, cps, sessions, now))
        groups.setdefault(_norm_agent(r.get("owner") or ""), []).append(r)
    return groups


def task_annotate(root: str, rows: list) -> list:
    """给任务行补看板注解（R8）：截止紧迫度、前置完成度、阻塞原因、租约状态。

    hub_tasks / hub_next / CLI 共用；一次批量取前置状态，不逐行查库。纯只读、原地补字段：
    _due_state(overdue/today/upcoming/invalid) / _due_days / _deps_info[{id,title,status}] /
    _block_note / _lease_state / _lease_left_h / _locks / _progress_age_h / _progress_stall /
    R27 时钟：_last_cp / _last_cp_age_h / _heartbeat_at / _heartbeat_age_h（心跳为 agent 级）。
    R30 饥饿：_age_days（可开工的 pending 建了几天）/ _starve_days（≥ TASK_STARVE_DAYS 时同上）。
    R87 审批时效：_approval_wait_h（认领后等批小时数）/ _approval_aging（≥ 阈值）。
    """
    now = datetime.datetime.now()
    today = now.date()
    cutoff = _claim_cutoff()
    with db_conn(root) as conn:
        ref = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, title, status FROM tasks")}
        lock_counts = {r["task_id"]: r["n"] for r in conn.execute(
            "SELECT task_id, COUNT(*) AS n FROM file_leases "
            "WHERE task_id>0 AND released_at='' AND expires_at>? GROUP BY task_id",
            (_now(),))}
        cps = _checkpoint_index(conn)
        sessions = _sessions_index(conn)
    for t in rows:
        due = (t.get("due") or "").strip()
        t["_due_state"], t["_due_days"] = "", None
        if due and t.get("status") not in TASK_TERMINAL:
            try:
                days = (datetime.date.fromisoformat(due[:10]) - today).days
                t["_due_days"] = days
                t["_due_state"] = ("overdue" if days < 0
                                   else "today" if days == 0 else "upcoming")
            except ValueError:
                t["_due_state"] = "invalid"
        # R42：退避/重试信息随看板一起给，避免「任务在排队」被误读成「没人干」。
        try:
            t["_attempts"] = int(t.get("attempts") or 0)
        except (TypeError, ValueError):
            t["_attempts"] = 0
        t["_last_error"] = (t.get("last_error") or "").strip()
        t["_retry_wait_min"] = retry_wait_minutes(t) if t.get("status") == "pending" else 0
        t["_defer_wait_h"] = defer_wait_hours(t) if t.get("status") == "pending" else 0.0
        t["_defer_reason"] = (t.get("defer_reason") or "").strip()
        t["_approval_required"] = bool(int(t.get("approval_required") or 0))
        t["_approved"] = bool((t.get("approved_by") or "").strip())
        t["_approved_by"] = (t.get("approved_by") or "").strip()
        # R100 退回留痕：最近一次退回的人/原因/时间 + 累计次数，看板与复核据此解释
        # 「为什么这单又回到待办」，不必翻 journal。
        t["_rejected_by"] = (t.get("rejected_by") or "").strip()
        t["_reject_note"] = (t.get("reject_note") or "").strip()
        try:
            t["_reject_count"] = int(t.get("reject_count") or 0)
        except (TypeError, ValueError):
            t["_reject_count"] = 0
        t["_rejected"] = t["_reject_count"] > 0
        t["_reject_age_h"] = _age_hours((t.get("rejected_at") or "").strip(), now)
        t["_approval_wait_h"] = None
        t["_approval_aging"] = False
        if (t.get("status") == "in_progress" and t["_approval_required"]
                and not t["_approved_by"]):
            wait_h = _age_hours((t.get("claimed_at") or "").strip(), now)
            if wait_h is not None:
                t["_approval_wait_h"] = wait_h
                t["_approval_aging"] = wait_h >= TASK_APPROVAL_AGING_H
        t["_deps_info"] = []
        for i in _parse_task_deps(t.get("deps") or ""):
            dep = ref.get(i)
            t["_deps_info"].append({"id": i,
                                    "title": (dep or {}).get("title", ""),
                                    "status": (dep or {}).get("status", "missing")})
        block = (t.get("blocked_by") or "").strip()
        if not block or t.get("status") not in ("blocked", "pending"):
            t["_block_note"] = ""
        elif t.get("status") == "blocked":
            t["_block_note"] = block
        else:
            t["_block_note"] = f"{block}（残留标记，可清除）"
        # R30 待办饥饿：pending、无显式阻塞、前置全完成，却建了很久没人认领。
        # 等依赖/等外部条件不是「没人干」而是「还干不了」，单独归 waiting/blocked。
        t["_age_days"] = None
        t["_starve_days"] = None
        if (t.get("status") == "pending" and not block
                and not t.get("_retry_wait_min")
                and not t.get("_defer_wait_h")
                and all(d["status"] == "done" for d in t["_deps_info"])):
            age_days = _age_hours(t.get("created"), now)
            if age_days is not None:
                t["_age_days"] = round(age_days / 24, 1)
                if t["_age_days"] >= TASK_STARVE_DAYS:
                    t["_starve_days"] = t["_age_days"]
        state, left = _task_lease(t, cutoff, now)
        t["_lease_state"], t["_lease_left_h"] = state, left
        t["_locks"] = lock_counts.get(t.get("id"), 0)
        t.update(_task_clock_fields(t, cps, sessions, now))
    return rows


def _task_priority(row: dict) -> int:
    """任务优先级归一（0 是合法急件，不能用 `or DEFAULT_PRIORITY` 吞掉）。"""
    try:
        return int(row.get("priority"))
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY


def task_starvation(root: str, days=None, limit: int = 20) -> dict:
    """R30 待办饥饿清单（只读）：能开工却长期没人认领的 pending 任务。

    与「等待」区分：前置未完成、显式 blocked_by 的任务不算饥饿——还干不了，不是没人干；
    前置 id 不存在的属于结构缺陷（tasks_health 已单列），同样不算。
    年龄取 created，时间不可解析不猜（跳过）。排序：最久优先，同龄急件优先。
    """
    try:
        window = float(days) if days is not None else float(TASK_STARVE_DAYS)
    except (TypeError, ValueError):
        window = float(TASK_STARVE_DAYS)
    rows = task_list(root, status="pending", limit=500)
    task_annotate(root, rows)
    starving = [r for r in rows
                if r.get("_age_days") is not None and r["_age_days"] >= window]
    starving.sort(key=lambda t: (-t["_age_days"], _task_priority(t), t["id"]))
    by_priority: dict = {}
    for r in starving:
        pr = _task_priority(r)
        by_priority[pr] = by_priority.get(pr, 0) + 1
    try:
        lim = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        lim = 20
    return {"starving": starving[:lim], "window_days": window,
            "oldest_days": starving[0]["_age_days"] if starving else None,
            "by_priority": by_priority,
            "counts": {"starving": len(starving)}}


# ---------------------------------------------------------------- R11 文件租约（咨询式）

def _file_lock_ttl(ttl_min) -> int:
    try:
        n = int(ttl_min)
    except (TypeError, ValueError):
        return FILE_LEASE_TTL_MIN
    return max(FILE_LEASE_TTL_FLOOR, min(n, FILE_LEASE_TTL_CAP))


def _file_lock_path(root: str, path: str) -> tuple:
    """路径规范化 → (错误或 "", 规范 key, 展示路径, 根内首段项目名或 "")。

    接受根内相对路径（相对数据根）与任意绝对路径（部署目录/agent 工作区也能锁）。
    key 用 normcase + 正斜杠统一大小写与分隔符——Windows 上同一文件的多种写法
    必须判成同一把锁，否则冲突检测形同虚设。"""
    raw = (path or "").strip().strip('"').strip("'")
    if not raw:
        return "path 必填", "", "", ""
    if "\x00" in raw:
        return "path 含非法字符 NUL", "", "", ""
    if len(raw) > 500:
        return f"path 过长（{len(raw)} > 500）", "", "", ""
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = os.path.join(root, expanded)
    absolute = os.path.abspath(os.path.normpath(expanded))
    root_abs = os.path.abspath(os.path.normpath(root))
    if os.path.normcase(absolute).rstrip("\\/") == os.path.normcase(root_abs).rstrip("\\/"):
        return "不能锁数据根目录本身（等于锁全机）；请锁具体文件或子目录", "", "", ""
    key = os.path.normcase(absolute).replace("\\", "/").rstrip("/")
    display = absolute.replace("\\", "/")
    project = ""
    try:
        rel = os.path.relpath(absolute, root_abs)
    except ValueError:
        rel = ""
    if rel and rel != ".." and not rel.startswith(".." + os.sep) and not os.path.isabs(rel):
        display = rel.replace(os.sep, "/")
        first = rel.split(os.sep)[0]
        # 根内首段是真实目录才算项目；根下散文件、_hub 等保留目录不冒充项目
        if first and not first.startswith("_") and os.path.isdir(os.path.join(root_abs, first)):
            project = first
    return "", key, display, project


def _file_lock_conflict(a: str, b: str) -> bool:
    """两个规范路径是否覆盖同一范围：相等，或互为祖先/子孙（锁目录=覆盖其下文件）。"""
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _file_lock_row(row: dict, now: datetime.datetime) -> dict:
    out = dict(row)
    left = None
    try:
        left = (datetime.datetime.fromisoformat(out["expires_at"]) - now).total_seconds() / 60
    except (TypeError, ValueError):
        left = None
    out["_expired"] = left is None or left <= 0
    out["_left_min"] = max(0, int(round(left))) if left is not None else 0
    out["_active"] = not out.get("released_at") and not out["_expired"]
    return out


def file_lease_acquire(root: str, path: str, agent: str, task_id=None,
                       ttl_min=FILE_LEASE_TTL_MIN, reason: str = "",
                       project: str = "") -> tuple:
    """R11 获取文件/目录租约（咨询式：不拦写盘，但让后来者看见冲突）。

    同一 agent 对同一路径再次 acquire = 续租（不新增行、fence 不变）。
    冲突判定含祖先/子孙；冲突时返回持有人、租约号、剩余分钟、任务与理由，供调用方协调。
    过期租约在 acquire 时顺带标记 released_by='expiry'，不阻塞后来者。
    挂任务（task_id）时必须「任务在办且本人持有」——租约跟任务生命周期走。
    返回 (错误或 "", 租约行 dict)。"""
    who = _norm_agent(agent)
    if not who:
        return "agent 必填（租约必须署名）", {}
    err, key, display, derived = _file_lock_path(root, path)
    if err:
        return err, {}
    ttl = _file_lock_ttl(ttl_min)
    tid = 0
    raw_tid = task_id.strip() if isinstance(task_id, str) else task_id
    if raw_tid not in (None, ""):
        try:
            tid = int(raw_tid)
        except (TypeError, ValueError):
            return f"任务 id 非法：{task_id!r}", {}
    now_dt = datetime.datetime.now()
    now = now_dt.isoformat(timespec="seconds")
    expires = (now_dt + datetime.timedelta(minutes=ttl)).isoformat(timespec="seconds")
    with db_conn(root) as conn:
        busy = _begin_write(conn)
        if busy:
            return busy, {}
        if tid:
            trow = conn.execute(
                "SELECT status,owner FROM tasks WHERE id=?", (tid,)).fetchone()
            if trow is None:
                return f"任务 #{tid} 不存在（文件租约必须挂真实任务）", {}
            if trow["status"] != "in_progress":
                return (f"任务 #{tid} 当前是 {TASK_CN.get(trow['status'], trow['status'])}；"
                        "先 hub_claim 认领，锁文件才挂得上"), {}
            if (trow["owner"] or "").strip() != who:
                return (f"任务 #{tid} 由 {trow['owner'] or '无主'} 持有；"
                        "只有持有者能给它锁文件（先 hub_claim）"), {}
        actives = [dict(r) for r in conn.execute(
            "SELECT * FROM file_leases WHERE released_at=''")]
        same_own, conflicts = [], []
        for r in actives:
            if not _file_lock_conflict(key, r["path"]):
                continue
            dec = _file_lock_row(r, now_dt)
            if not dec["_active"]:
                conn.execute(
                    "UPDATE file_leases SET released_at=?, released_by='expiry' "
                    "WHERE id=? AND released_at=''", (now, r["id"]))
            elif r["owner"] == who:
                same_own.append(r)
            else:
                conflicts.append(dec)
        if conflicts:
            c = conflicts[0]
            more = f"；另有 {len(conflicts) - 1} 条冲突" if len(conflicts) > 1 else ""
            tid_s = f"｜任务 #{c['task_id']}" if c["task_id"] else ""
            reason_s = f"｜{c['reason'][:60]}" if c["reason"] else ""
            return (f"文件被占用：{c['display_path'] or c['path']} 由 {c['owner']} 持有"
                    f"（租约 #{c['id']}，剩 {c['_left_min']} 分钟{tid_s}{reason_s}）{more}。"
                    "不要并发编辑——等其释放、改走别的文件，或 hub_signal_send 协调。"), c
        for r in same_own:
            if r["path"] == key:  # 同路径再 acquire = 续租
                conn.execute(
                    "UPDATE file_leases SET renewed_at=?, expires_at=?, ttl_min=?, "
                    "task_id=?, reason=? WHERE id=?",
                    (now, expires, ttl, tid or r["task_id"],
                     (reason or "").strip()[:200] or r["reason"], r["id"]))
                row = dict(conn.execute(
                    "SELECT * FROM file_leases WHERE id=?", (r["id"],)).fetchone())
                out = _file_lock_row(row, now_dt)
                out["_renewed"] = True
                _journal_row(conn, who, "续租文件", f"lock#{r['id']}",
                             f"{display}｜TTL {ttl}min")
                return "", out
        fence = conn.execute(
            "SELECT COALESCE(MAX(fence),0)+1 FROM file_leases WHERE path=?",
            (key,)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO file_leases(path,display_path,project,owner,task_id,reason,ttl_min,"
            "fence,acquired_at,renewed_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (key, display, (project or "").strip()[:200] or derived, who, tid,
             (reason or "").strip()[:200], ttl, fence, now, now, expires))
        lid = cur.lastrowid
        row = dict(conn.execute(
            "SELECT * FROM file_leases WHERE id=?", (lid,)).fetchone())
        _journal_row(conn, who, "获取文件租约", f"lock#{lid}",
                     f"{display}｜TTL {ttl}min" + (f"｜tasks#{tid}" if tid else ""))
        return "", _file_lock_row(row, now_dt)


def file_lease_renew(root: str, lease_id, agent: str, ttl_min=None) -> tuple:
    """R11 续租（仅持有者）。过期但未被别人抢走仍可续——任务还在自己手里时不该因
    忘了续锁而被自己锁在门外；已被他人持有则拒绝并说明冲突。"""
    who = _norm_agent(agent)
    if not who:
        return "agent 必填（续租必须署名）", {}
    try:
        lid = int(lease_id)
    except (TypeError, ValueError):
        return f"租约 id 非法：{lease_id!r}", {}
    now_dt = datetime.datetime.now()
    now = now_dt.isoformat(timespec="seconds")
    with db_conn(root) as conn:
        busy = _begin_write(conn)
        if busy:
            return busy, {}
        row = conn.execute("SELECT * FROM file_leases WHERE id=?", (lid,)).fetchone()
        if row is None:
            return f"租约 #{lid} 不存在", {}
        row = dict(row)
        if row["released_at"]:
            return (f"租约 #{lid} 已释放（{row['released_by'] or '?'}｜"
                    f"{row['released_at'][:16]}）；需要时重新 acquire"), row
        if row["owner"] != who:
            return f"租约 #{lid} 由 {row['owner']} 持有，只有持有者可续租", row
        for other in [dict(r) for r in conn.execute(
                "SELECT * FROM file_leases WHERE released_at='' AND id<>?", (lid,))]:
            if other["owner"] == who or not _file_lock_conflict(row["path"], other["path"]):
                continue
            dec = _file_lock_row(other, now_dt)
            if dec["_active"]:
                return (f"续租失败：{row['display_path']} 已被 {other['owner']} 持有"
                        f"（租约 #{other['id']}，剩 {dec['_left_min']} 分钟）；改走协调"), other
        ttl = _file_lock_ttl(ttl_min if ttl_min is not None else row["ttl_min"])
        expires = (now_dt + datetime.timedelta(minutes=ttl)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE file_leases SET renewed_at=?, expires_at=?, ttl_min=? "
            "WHERE id=? AND released_at=''", (now, expires, ttl, lid))
        fresh = dict(conn.execute("SELECT * FROM file_leases WHERE id=?", (lid,)).fetchone())
        _journal_row(conn, who, "续租文件", f"lock#{lid}",
                     f"{fresh['display_path']}｜TTL {ttl}min")
        return "", _file_lock_row(fresh, now_dt)


def file_lease_release(root: str, lease_id=None, path: str = "", agent: str = "",
                       reason: str = "") -> tuple:
    """R11 释放（仅持有者）：lease_id 精确释放；只给 path 则释放自己在该路径的活跃租约。
    返回 (错误或 "", 释放条数)。fence 保留——历史租约仍是迟到写入的对照凭据。"""
    who = _norm_agent(agent)
    if not who:
        return "agent 必填（释放必须署名，防他人代释放）", 0
    now = _now()
    with db_conn(root) as conn:
        busy = _begin_write(conn)
        if busy:
            return busy, 0
        if lease_id not in (None, ""):
            try:
                lid = int(lease_id)
            except (TypeError, ValueError):
                return f"租约 id 非法：{lease_id!r}", 0
            row = conn.execute("SELECT * FROM file_leases WHERE id=?", (lid,)).fetchone()
            if row is None:
                return f"租约 #{lid} 不存在", 0
            if row["owner"] != who:
                return f"租约 #{lid} 由 {row['owner']} 持有，不能代释放", 0
            if row["released_at"]:
                return (f"租约 #{lid} 已于 {row['released_at'][:16]} 释放"
                        f"（{row['released_by'] or '?'}）"), 0
            conn.execute(
                "UPDATE file_leases SET released_at=?, released_by=? "
                "WHERE id=? AND released_at=''", (now, who, lid))
            _journal_row(conn, who, "释放文件租约", f"lock#{lid}",
                         f"{row['display_path']}" + (f"｜{reason[:80]}" if reason else ""))
            return "", 1
        err, key, display, _ = _file_lock_path(root, path)
        if err:
            return err, 0
        cur = conn.execute(
            "UPDATE file_leases SET released_at=?, released_by=? "
            "WHERE path=? AND owner=? AND released_at=''", (now, who, key, who))
        if not cur.rowcount:
            return f"没有找到你在 {display} 上的活跃租约（用 action=list 查看）", 0
        _journal_row(conn, who, "释放文件租约", f"path:{display}",
                     f"{cur.rowcount} 个" + (f"｜{reason[:80]}" if reason else ""))
        return "", cur.rowcount


def file_lease_prune(root: str, agent: str = "", limit: int = 200) -> tuple:
    """R13 回收过期未释放的租约（安全 GC）：只动 `released_at=''` 且已过期的行。

    为什么需要：过期租约本身不拦人（check/acquire 只认未过期），但没人再 acquire
    同一路径时它们会永久留在台账里——巡检只能报数、CLI 又不能代他人释放，形成
    「看得见清不掉」的卫生死角。回收只把行标记为 released_by='expiry'，保留记录
    （fence/历史仍是迟到写入的对照凭据）。活跃租约**永不触碰**。
    返回 (错误或 "", 被回收的行列表)；重复调用幂等。
    """
    who = _norm_agent(agent) or "system"
    now = _now()
    try:
        cap = max(1, min(int(limit or 200), 500))
    except (TypeError, ValueError):
        cap = 200
    with db_conn(root) as conn:
        busy = _begin_write(conn)
        if busy:
            return busy, []
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM file_leases WHERE released_at='' AND expires_at<=? "
            "ORDER BY id LIMIT ?", (now, cap))]
        if not rows:
            return "", []
        # 只统计真正更新成功的行：并发 prune 时另一进程可能已先释放（rowcount=0），
        # 把它计入回报/审计会制造「回收了 N 条」的假账。
        released = []
        for r in rows:
            cur = conn.execute(
                "UPDATE file_leases SET released_at=?, released_by='expiry' "
                "WHERE id=? AND released_at=''", (now, r["id"]))
            if cur.rowcount:
                r["released_at"], r["released_by"] = now, "expiry"
                released.append(r)
        if not released:
            return "", []
        ids = "、".join(f"#{r['id']}" for r in released[:10])
        more = f" 等 {len(released)} 条" if len(released) > 10 else ""
        _journal_row(conn, who, "回收过期文件租约", "expired",
                     f"{len(released)} 条：{ids}{more}")
    return "", released


def file_lease_list(root: str, path: str = "", project: str = "", owner: str = "",
                    include_released: bool = False, limit: int = 50) -> list:
    """R11 租约台账（新在前）：默认只看未释放（含已过期未清理的，标 _expired）。
    传 path 按规范化后的**同一路径**过滤；project/owner 同样可过滤。"""
    q = "SELECT * FROM file_leases WHERE 1=1"
    args: list = []
    if path:
        err, key, _, _ = _file_lock_path(root, path)
        if err:
            return []
        q += " AND path=?"
        args.append(key)
    if project:
        q += " AND project=?"
        args.append(project)
    if owner:
        q += " AND owner=?"
        args.append(_norm_agent(owner))
    if not include_released:
        q += " AND released_at=''"
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 50), 200)))
    now = datetime.datetime.now()
    with db_conn(root) as conn:
        return [_file_lock_row(dict(r), now) for r in conn.execute(q, args)]


def file_lease_count(root: str, path: str = "", project: str = "",
                     owner: str = "", include_released: bool = False) -> int:
    """租约总数（R273）：与 file_lease_list 同过滤口径，列表抬头用它报总数。"""
    q = "SELECT COUNT(*) FROM file_leases WHERE 1=1"
    args: list = []
    if path:
        err, key, _, _ = _file_lock_path(root, path)
        if err:
            return 0
        q += " AND path=?"
        args.append(key)
    if project:
        q += " AND project=?"
        args.append(project)
    if owner:
        q += " AND owner=?"
        args.append(_norm_agent(owner))
    if not include_released:
        q += " AND released_at=''"
    with db_conn(root) as conn:
        return int(conn.execute(q, args).fetchone()[0])


def file_lease_check(root: str, path: str) -> dict:
    """R11 只读查占：某路径当前是否被谁持有（冲突含祖先/子孙）。不写库。"""
    err, key, display, derived = _file_lock_path(root, path)
    if err:
        return {"error": err, "path": "", "display_path": "", "project": "", "free": None,
                "holders": []}
    now = datetime.datetime.now()
    fresh = now.isoformat(timespec="seconds")
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM file_leases WHERE released_at='' AND expires_at>?", (fresh,))]
        project = derived
        for r in rows:
            if _file_lock_conflict(key, r["path"]):
                project = r["project"] or project
                break
        holders = []
        for r in rows:
            if not _file_lock_conflict(key, r["path"]):
                continue
            dec = _file_lock_row(r, now)
            if dec["_active"]:
                holders.append(dec)
    return {"path": key, "display_path": display, "project": project,
            "free": not holders, "holders": holders}


def file_lease_renew_for_agent(root: str, agent: str) -> int:
    """R11 心跳续租：只续「挂在本人正在进行任务上」的租约。返回续租条数。

    未挂任务的租约**不**自动续——一把忘记释放的锁若被心跳永久续命，反而制造死锁。
    """
    who = _norm_agent(agent)
    if not who:
        return 0
    now_dt = datetime.datetime.now()
    now = now_dt.isoformat(timespec="seconds")
    renewed = 0
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT l.* FROM file_leases l JOIN tasks t ON l.task_id=t.id "
            "WHERE l.owner=? AND l.released_at='' AND l.task_id>0 "
            "AND t.status='in_progress' AND t.owner=?", (who, who))]
        for row in rows:
            expires = (now_dt + datetime.timedelta(
                minutes=_file_lock_ttl(row["ttl_min"]))).isoformat(timespec="seconds")
            cur = conn.execute(
                "UPDATE file_leases SET renewed_at=?, expires_at=? "
                "WHERE id=? AND released_at=''", (now, expires, row["id"]))
            renewed += cur.rowcount
    return renewed


def file_lease_project_active(root: str, project: str, exclude_agent: str = "",
                              limit: int = 5) -> list:
    """R11 心跳提示：本项目当前被**其他 agent** 持有的活跃文件租约（按到期先后）。"""
    if not project:
        return []
    now_dt = datetime.datetime.now()
    now = now_dt.isoformat(timespec="seconds")
    who = _norm_agent(exclude_agent)
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM file_leases WHERE project=? AND released_at='' "
            "AND expires_at>? AND owner<>? ORDER BY expires_at ASC LIMIT ?",
            (project, now, who, max(1, min(int(limit or 5), 20))))]
    return [_file_lock_row(r, now_dt) for r in rows]


def _release_leases_for_task(conn, task_id, agent: str, reason: str = "") -> int:
    """R11 任务生命周期联动：任务离开本人手中时，挂在它上面的文件租约一并释放。
    与任务流转同事务，供 task_claim 接管 / task_update 流转（done/pending/blocked/换人）调用。"""
    try:
        tid = int(task_id or 0)
    except (TypeError, ValueError):
        return 0
    if not tid:
        return 0
    cur = conn.execute(
        "UPDATE file_leases SET released_at=?, released_by=? "
        "WHERE task_id=? AND released_at=''",
        (_now(), _norm_agent(agent) or "system", tid))
    if cur.rowcount:
        _journal_row(conn, agent, "释放任务文件租约", f"tasks#{tid}",
                     f"{cur.rowcount} 个｜{reason[:80]}")
    return cur.rowcount


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
        # R35：任务绑定记录优先收编——不受创建时间窗口限制（跨天续跑、先写记录
        # 后建任务都不该丢）；未绑定记录仍按「同项目 + 任务创建后」聚合，但排除已属
        # 其它任务的记录，避免把别人的工作算进本任务结晶。
        linked = [dict(r) for r in conn.execute(
            "SELECT id, agent, date, title, task_id FROM records WHERE status='active' "
            "AND record_type!='crystal' AND task_id=? AND project=? "
            "ORDER BY id ASC LIMIT 50", (tid, row["project"]))]
        rec_sql = ("SELECT id, agent, date, title, task_id FROM records WHERE project=? "
                   "AND status='active' AND record_type!='crystal' AND task_id=0 AND created>=?")
        rec_args: list = [row["project"], row["created"] or ""]
        if row.get("done_at"):
            rec_sql += " AND created<=?"
            rec_args.append(row["done_at"])
        rec_sql += " ORDER BY id ASC LIMIT 50"
        recs = linked + [dict(r) for r in conn.execute(rec_sql, rec_args)]
        cp_count = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id=? AND kind!='auto_claim'",
            (tid,)).fetchone()[0]
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
        lines.append(f"关联记录 {len(recs)} 条（任务绑定 {len(linked)} 条 + 同项目同期 "
                     f"{len(recs) - len(linked)} 条；原条目不删，可追溯）：")
        lines.append("")
        for r in recs:
            who = f"[{r['agent']}] " if r.get("agent") else ""
            bound = " ·绑定" if (r.get("task_id") or 0) == tid else ""
            lines.append(f"- #{r['id']} {who}{(r.get('date') or '')[:10]} "
                         f"{(r.get('title') or '')[:70]}{bound}")
    else:
        lines.append("关联记录 0 条（任务本身即最小结晶）。")
    rid = add_record(root, row["project"], owner, (row.get("done_at") or _now())[:10],
                     f"{CRYSTAL_PREFIX}{tid}：{row['title'][:60]}", "\n".join(lines),
                     source="hub", record_type="crystal", task_id=tid)
    for oid in olds:  # force 重做：软删旧结晶（新结晶已落库；add 失败时旧结晶保留）
        with db_conn(root) as conn2:
            conn2.execute("UPDATE records SET status='deleted', valid_until=? WHERE id=?",
                          (_now()[:10], oid))
    summary = (f"结晶完成：记录 #{rid}（关联 {len(recs)} 条）｜任务 #{tid}「{row['title'][:40]}」")
    if not recs and not cp_count:
        summary += ("｜⚠️ 无完成证据：任务期间没有工作记录或手工检查点，"
                    "结晶只有任务卡——下次完成前先 hub_checkpoint 或 hub_log_work")
    return "", summary


# R35（2026-10-23）闭环漂移口径：记录说「已办完」、任务却还开着——这是任务闭环
# 最真实的失真面（真实库实测：完成记录躺在库中，任务 #2 仍挂着过期租约）。
# 负向短语先剔除再判，避免把「尚未完成/未交付」误报成完成声明。
_DONE_CLAIM_RE = re.compile(
    r"已(?:勾销|关闭|完成|交付|收尾|结项)|完成并(?:交付|验证|发布)|关单|"
    r"(?<![a-z])(?:done|closed|finished|completed)(?![a-z])", re.IGNORECASE)
_DONE_NEG_RE = re.compile(
    r"(?:尚未|还未|还没|未|没|无法|不能|未能|无需|不需要|待)(?:完全)?"
    r"(?:勾销|关闭|完成|交付|收尾|结项)", re.IGNORECASE)
_TASK_REF_RE = re.compile(r"(?:任务|task)\s*#\s*(\d+)", re.IGNORECASE)


def _claims_done(text: str) -> bool:
    return bool(_DONE_CLAIM_RE.search(_DONE_NEG_RE.sub("", text or "")))


def _drift_snippet(text: str, width: int = 80) -> str:
    clean = _DONE_NEG_RE.sub("", text or "")
    m = _DONE_CLAIM_RE.search(clean)
    if not m:
        return (clean or "").strip().replace("\n", " ")[:width]
    start = max(0, m.start() - 20)
    return clean[start:start + width].replace("\n", " ").strip()


def task_closure_drift(root: str, days: int = 30, limit: int = 20) -> dict:
    """R35 完成声明未关单巡检：记录里写了「已完成/已交付/关单」但任务还没 done。
    两类来源：① 绑定记录（records.task_id>0）；② 未绑定的历史记录正文点名「任务 #N」。
    同一任务只报最新一条；负向表述先剔除；只读，不改任何任务状态。
    返回 {counts:{total,linked,unlinked}, window_days, items:[...]}。"""
    try:
        days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days = 30
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)) \
        .isoformat(timespec="seconds")
    with db_conn(root) as conn:
        linked = [dict(r) for r in conn.execute(
            "SELECT r.id,r.project,r.agent,r.date,r.title,r.content,r.created,r.task_id,"
            "t.status AS task_status,t.owner AS task_owner,t.title AS task_title "
            "FROM records r JOIN tasks t ON t.id=r.task_id "
            "WHERE r.status='active' AND r.record_type!='crystal' AND r.task_id>0 "
            "AND t.status NOT IN ('done','cancelled','dead') AND r.created>=? "
            "ORDER BY r.id DESC", (cutoff,))]
        unlinked = [dict(r) for r in conn.execute(
            "SELECT id,project,agent,date,title,content,created,task_id FROM records "
            "WHERE status='active' AND record_type!='crystal' AND task_id=0 AND created>=? "
            "ORDER BY id DESC LIMIT 1000", (cutoff,))]
        tasks = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id,status,owner,title FROM tasks")}
    items: dict = {}
    for r in linked:
        text = f"{r['title']}\n{r['content']}"
        if not _claims_done(text):
            continue
        items.setdefault(r["task_id"], {
            "task_id": r["task_id"], "task_status": r["task_status"],
            "task_owner": r["task_owner"] or "", "task_title": r["task_title"] or "",
            "record_id": r["id"], "record_title": r["title"] or "",
            "agent": r["agent"] or "", "project": r["project"] or "",
            "created": r["created"] or "", "linked": True,
            "snippet": _drift_snippet(r["content"])})
    for r in unlinked:
        text = f"{r['title']}\n{r['content']}"
        if not _claims_done(text):
            continue
        for ref in _TASK_REF_RE.findall(text):
            tid = int(ref)
            trow = tasks.get(tid)
            if not trow or trow["status"] == "done" or tid in items:
                continue
            items[tid] = {
                "task_id": tid, "task_status": trow["status"],
                "task_owner": trow["owner"] or "", "task_title": trow["title"] or "",
                "record_id": r["id"], "record_title": r["title"] or "",
                "agent": r["agent"] or "", "project": r["project"] or "",
                "created": r["created"] or "", "linked": False,
                "snippet": _drift_snippet(text)}
    ordered = sorted(items.values(), key=lambda x: (x["created"], x["record_id"]),
                     reverse=True)[:limit]
    return {"counts": {
        "total": len(items),
        "linked": sum(1 for x in items.values() if x["linked"]),
        "unlinked": sum(1 for x in items.values() if not x["linked"])},
        "window_days": days, "items": ordered}


def _seconds_between(a: str, b: str):
    try:
        return (datetime.datetime.fromisoformat((b or "").strip())
                - datetime.datetime.fromisoformat((a or "").strip())).total_seconds()
    except (TypeError, ValueError):
        return None


def _pct(values: list, p: float):
    """线性插值分位数（空列表返回 None）。任务周期长尾很重，只报均值会被单条卡死单拉偏。"""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return round(xs[0], 2)
    k = (len(xs) - 1) * max(0.0, min(1.0, p))
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


def _hours_stat(rows: list, start_key: str, end_key: str) -> dict:
    vals = []
    for r in rows:
        secs = _seconds_between(r.get(start_key, ""), r.get(end_key, ""))
        if secs is not None and secs >= 0:
            vals.append(secs / 3600.0)
    if not vals:
        return {"n": 0, "p50": None, "p90": None, "avg": None, "max": None}
    return {"n": len(vals), "p50": _pct(vals, 0.5), "p90": _pct(vals, 0.9),
            "avg": round(sum(vals) / len(vals), 2), "max": round(max(vals), 2)}


def task_metrics(root: str, days: int = 7, project: str = "", agent: str = "") -> dict:
    """任务过程统计（只读）：回答「队列在流动吗、卡在哪、谁在拖」。

    口径：
    · 流量/周期/质量是窗口口径（回看 N 天）；在办/重试/风险是**当前快照**。
    · 完成周期 = done_at - created；审批耗时 = approved_at - created（任务表没有
      「申请审批时刻」，这是近似值，报告里明说）。
    · 完成质量只抽检窗口内最近 30 条完成单：够看趋势，不做全量慢查。
    · 分母为 0 时返回 0/None，不编造比率。"""
    try:
        days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days = 7
    project = (project or "").strip()
    agent = _norm_agent(agent)
    now = datetime.datetime.now()
    cutoff = (now - datetime.timedelta(days=days)).isoformat(timespec="seconds")
    claim_cut = _claim_cutoff()
    today = now.date().isoformat()
    base = []
    base_args: list = []
    if project:
        base.append("project=?")
        base_args.append(project)
    if agent:
        base.append("LOWER(TRIM(COALESCE(owner,''))) = ?")
        base_args.append(agent)

    with db_conn(root) as conn:
        def where(extra=(), extra_args=()):
            cs = base + list(extra)
            return ((" WHERE " + " AND ".join(cs)) if cs else ""), base_args + list(extra_args)

        def one(sql, extra=(), extra_args=()):
            w, a = where(extra, extra_args)
            return conn.execute(sql + w, a).fetchone()[0]

        def all_rows(sql, extra=(), extra_args=()):
            w, a = where(extra, extra_args)
            return [dict(r) for r in conn.execute(sql + w, a)]

        created = one("SELECT COUNT(*) FROM tasks", ["created>=?"], [cutoff])
        done_rows = all_rows(
            "SELECT id,project,title,owner,created,done_at FROM tasks",
            ["status='done'", "done_at>=?"], [cutoff])
        approved_rows = all_rows(
            "SELECT id,project,title,owner,created,approved_at FROM tasks",
            ["approved_at>=?"], [cutoff])
        cancelled = one(
            "SELECT COUNT(*) FROM tasks",
            ["status IN ('cancelled','dead')",
             "COALESCE(NULLIF(cancelled_at,''), NULLIF(updated,''), '')>=?"], [cutoff])
        by_status = {s: 0 for s in TASK_STATUSES}
        bw, ba = where()
        for r in conn.execute(
                "SELECT status, COUNT(*) n FROM tasks" + bw + " GROUP BY status", ba):
            if r["status"] in by_status:
                by_status[r["status"]] = r["n"]
        wip_rows = all_rows(
            "SELECT id,title,owner,claimed_at,due,approval_required,approved_by,attempts "
            "FROM tasks", ["status='in_progress'"])
        rw, ra = where(["attempts>0"])
        rr = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(attempts),0) s, COALESCE(MAX(attempts),0) m "
            "FROM tasks" + rw, ra).fetchone()
        aw, aa = where(["owner!=''"])
        agent_rows = [dict(r) for r in conn.execute(
            "SELECT LOWER(TRIM(COALESCE(owner,''))) ag,"
            " SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END) doing,"
            " SUM(CASE WHEN status='done' AND done_at>=? THEN 1 ELSE 0 END) done,"
            " SUM(CASE WHEN status='dead' AND COALESCE(NULLIF(updated,''),'')>=? THEN 1 ELSE 0 END) dead,"
            " COALESCE(SUM(attempts),0) attempts"
            " FROM tasks" + aw + " GROUP BY ag ORDER BY doing DESC, done DESC",
            [cutoff, cutoff] + aa)]
        dead_rows = all_rows(
            "SELECT id,title,owner FROM tasks",
            ["status='dead'", "COALESCE(NULLIF(updated,''),'')>=?"], [cutoff])

    stale = [t for t in wip_rows
             if (t.get("claimed_at") or "").strip() and t["claimed_at"] < claim_cut]
    unowned = [t for t in wip_rows if not (t.get("owner") or "").strip()]
    waiting = [t for t in wip_rows if int(t.get("approval_required") or 0)
               and not (t.get("approved_by") or "").strip()]
    approval_aging = [t for t in waiting
                      if (h := _age_hours((t.get("claimed_at") or "").strip(), now))
                      is not None and h >= TASK_APPROVAL_AGING_H]
    overdue = [t for t in wip_rows if (t.get("due") or "").strip()
               and (t.get("due") or "")[:10] < today]
    high_retry = [t for t in wip_rows if int(t.get("attempts") or 0) >= 2]

    quality: dict
    if agent:
        quality = {"skipped": "agent 过滤下不抽检完成质量（质量巡检按项目维度）"}
    else:
        try:
            q = task_closure_quality_report(root, days=days, project=project, limit=30)
            quality = dict(q.get("counts") or {})
        except Exception as e:  # noqa: BLE001 统计不因质检失败整体报错
            quality = {"error": f"{type(e).__name__}: {str(e)[:80]}"}

    risks = []
    if stale:
        risks.append({"kind": "lease_stale", "count": len(stale),
                      "sample": [f"#{t['id']} {t['title'][:24]}" for t in stale[:3]]})
    if unowned:
        risks.append({"kind": "unowned", "count": len(unowned),
                      "sample": [f"#{t['id']} {t['title'][:24]}" for t in unowned[:3]]})
    if waiting:
        risks.append({"kind": "approval_waiting", "count": len(waiting),
                      "sample": [f"#{t['id']} {t['title'][:24]}" for t in waiting[:3]]})
    if approval_aging:
        risks.append({"kind": "approval_aging", "count": len(approval_aging),
                      "sample": [f"#{t['id']} {t['title'][:24]}"
                                 f"（等批 {_age_hours((t.get('claimed_at') or '').strip(), now):g}h）"
                                 for t in approval_aging[:3]]})
    if overdue:
        risks.append({"kind": "overdue", "count": len(overdue),
                      "sample": [f"#{t['id']} {t['title'][:24]}（{t['due'][:10]}）" for t in overdue[:3]]})
    if dead_rows:
        risks.append({"kind": "dead_letter", "count": len(dead_rows),
                      "sample": [f"#{t['id']} {t['title'][:24]}" for t in dead_rows[:3]]})
    if high_retry:
        risks.append({"kind": "retry_storm", "count": len(high_retry),
                      "sample": [f"#{t['id']} 第{t['attempts']}次尝试" for t in high_retry[:3]]})

    return {
        "window_days": days, "project": project, "agent": agent,
        "now": now.isoformat(timespec="seconds"),
        "claim_ttl_hours": CLAIM_TTL_HOURS,
        "flow": {"created": created, "done": len(done_rows),
                 "cancelled_or_dead": cancelled, "approved": len(approved_rows),
                 "done_cycle_hours": _hours_stat(done_rows, "created", "done_at"),
                 "approval_hours": _hours_stat(approved_rows, "created", "approved_at")},
        "by_status": by_status,
        "wip": {"in_progress": len(wip_rows), "approval_waiting": len(waiting),
                "approval_aging": len(approval_aging),
                "stale_leases": len(stale), "unowned": len(unowned), "overdue": len(overdue)},
        "retry": {"tasks_with_attempts": rr["n"] or 0,
                  "attempts_total": rr["s"] or 0, "max_attempts": rr["m"] or 0},
        "agents": [a for a in agent_rows if a["ag"]],
        "quality": quality,
        "risks": risks,
        "notes": ["完成周期=done_at-created；审批耗时=approved_at-created（无申请时刻，近似）",
                  "在办/重试/风险是当前快照；流量/周期/质量是窗口口径"],
    }


def team_digest(root: str, days: int = 1) -> dict:
    """团队日结（只读）：窗口流动 + 当前风险 + 按 agent 记分卡。

    与 task_metrics 分工：那个回答「任务过程是否顺畅」，这里回答「这段时间谁做了什么、
    现在该谁处理什么」。流量按窗口（记录 date / 其余 created·ts）；在办、租约、审批、
    未闭错误是当前快照，不随窗口缩水。空归属单列在风险里，不硬塞给某个 agent。"""
    try:
        days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days = 1
    now = datetime.datetime.now()
    cutoff = (now - datetime.timedelta(days=days)).isoformat(timespec="seconds")
    start_date = (now - datetime.timedelta(days=days)).date().isoformat()
    claim_cut = _claim_cutoff()
    today = now.date().isoformat()
    sec = security_snapshot(root, days=days)

    def clean(name) -> str:
        k = _norm_agent(name)
        return k if k not in ("", "unknown", "user", "migrated", "-") else ""

    with db_conn(root) as conn:
        reg = {}
        for r in conn.execute(
                "SELECT name,control_state,control_note,last_seen,heartbeats,skills FROM agents"):
            k = clean(r["name"])
            if k:
                reg[k] = dict(r)
        rec = {}
        for r in conn.execute(
                "SELECT LOWER(TRIM(COALESCE(agent,''))) ag, COUNT(*) n,"
                " COUNT(DISTINCT COALESCE(NULLIF(date,''), substr(created,1,10))) days,"
                " MAX(COALESCE(NULLIF(date,''), substr(created,1,10))) last"
                " FROM records WHERE status='active'"
                " AND COALESCE(NULLIF(date,''), substr(created,1,10))>=?"
                " GROUP BY ag", (start_date,)):
            if clean(r["ag"]):
                rec[r["ag"]] = dict(r)
        mem = {r["ag"]: r["n"] for r in conn.execute(
            "SELECT LOWER(TRIM(COALESCE(agent,''))) ag, COUNT(*) n FROM memories"
            " WHERE status='active' AND created>=? GROUP BY ag", (cutoff,)) if clean(r["ag"])}
        task = {}
        for r in conn.execute(
                "SELECT LOWER(TRIM(COALESCE(owner,''))) ag,"
                " SUM(CASE WHEN created>=? THEN 1 ELSE 0 END) created_n,"
                " SUM(CASE WHEN status='done' AND done_at>=? THEN 1 ELSE 0 END) done_n,"
                " SUM(CASE WHEN status NOT IN ('done','cancelled','dead') THEN 1 ELSE 0 END) open_n,"
                " SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END) doing,"
                " SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) blocked,"
                " SUM(CASE WHEN status='dead' AND COALESCE(NULLIF(updated,''),'')>=?"
                "     THEN 1 ELSE 0 END) dead_n,"
                " SUM(CASE WHEN status='in_progress' AND COALESCE(claimed_at,'')!=''"
                "     AND claimed_at<? THEN 1 ELSE 0 END) stale,"
                " SUM(CASE WHEN status='in_progress' AND approval_required=1"
                "     AND COALESCE(approved_by,'')='' THEN 1 ELSE 0 END) waiting,"
                " SUM(CASE WHEN status='in_progress' AND COALESCE(due,'')!=''"
                "     AND substr(due,1,10)<? THEN 1 ELSE 0 END) overdue,"
                " COALESCE(SUM(attempts),0) attempts"
                " FROM tasks GROUP BY ag",
                (cutoff, cutoff, cutoff, claim_cut, today)):
            if clean(r["ag"]):
                task[r["ag"]] = dict(r)
        err = {}
        for r in conn.execute(
                "SELECT LOWER(TRIM(COALESCE(agent,''))) ag,"
                " SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open_n,"
                " SUM(CASE WHEN ts>=? THEN 1 ELSE 0 END) opened"
                " FROM errors GROUP BY ag", (cutoff,)):
            if clean(r["ag"]):
                err[r["ag"]] = dict(r)
        calls = {r["ag"]: r["n"] for r in conn.execute(
            "SELECT LOWER(TRIM(COALESCE(agent,''))) ag, COUNT(*) n FROM tool_calls"
            " WHERE ts>=? GROUP BY ag", (cutoff,)) if clean(r["ag"])}
        search = {}
        for r in conn.execute(
                "SELECT LOWER(TRIM(COALESCE(agent,''))) ag, COUNT(*) n,"
                " SUM(CASE WHEN COALESCE(hits,0)=0 THEN 1 ELSE 0 END) zero"
                " FROM searches WHERE ts>=? GROUP BY ag", (cutoff,)):
            if clean(r["ag"]):
                search[r["ag"]] = dict(r)
        open_rows = [dict(r) for r in conn.execute(
            "SELECT id,title,status,owner,claimed_at,due,approval_required,approved_by,attempts"
            " FROM tasks WHERE status NOT IN ('done','cancelled','dead') ORDER BY priority, id")]
        dead_rows = [dict(r) for r in conn.execute(
            "SELECT id,title,owner FROM tasks WHERE status='dead'"
            " AND COALESCE(NULLIF(updated,''),'')>=?", (cutoff,))]
        handoff_rows = [dict(r) for r in conn.execute(
            "SELECT id, project, agent, status, created FROM handoffs WHERE status='open'")]
        by_status = {s: 0 for s in TASK_STATUSES}
        for r in conn.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status"):
            if r["status"] in by_status:
                by_status[r["status"]] = r["n"]

        def count(sql, args=()):
            return conn.execute(sql, args).fetchone()[0]

        flow = {
            "records": count(
                "SELECT COUNT(*) FROM records WHERE status='active'"
                " AND COALESCE(NULLIF(date,''), substr(created,1,10))>=?", (start_date,)),
            "memories": count(
                "SELECT COUNT(*) FROM memories WHERE status='active' AND created>=?", (cutoff,)),
            "errors_opened": count("SELECT COUNT(*) FROM errors WHERE ts>=?", (cutoff,)),
            "errors_open": count("SELECT COUNT(*) FROM errors WHERE status='open'"),
            "tool_calls": count("SELECT COUNT(*) FROM tool_calls WHERE ts>=?", (cutoff,)),
            "searches": count("SELECT COUNT(*) FROM searches WHERE ts>=?", (cutoff,)),
            "zero_hit_searches": count(
                "SELECT COUNT(*) FROM searches WHERE ts>=? AND COALESCE(hits,0)=0", (cutoff,)),
        }
        zero_samples = [dict(r) for r in conn.execute(
            "SELECT agent, query, COUNT(*) n FROM searches WHERE ts>=?"
            " AND COALESCE(hits,0)=0 GROUP BY agent, query ORDER BY n DESC, query LIMIT 5",
            (cutoff,))]
        done_rows = [dict(r) for r in conn.execute(
            "SELECT created, done_at FROM tasks WHERE status='done' AND done_at>=?", (cutoff,))]
        flow["tasks_created"] = count("SELECT COUNT(*) FROM tasks WHERE created>=?", (cutoff,))
        flow["tasks_done"] = len(done_rows)
        flow["tasks_cancelled_or_dead"] = count(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('cancelled','dead')"
            " AND COALESCE(NULLIF(cancelled_at,''), NULLIF(updated,''), '')>=?", (cutoff,))
        flow["done_cycle_p50_h"] = _hours_stat(done_rows, "created", "done_at")["p50"]

    handoff_annotate(handoff_rows, now)
    handoff_stale = [h for h in handoff_rows if h["_stale"]]
    online = {clean(s.get("agent")) for s in active_sessions(root)} - {""}
    names = set(reg) | set(rec) | set(mem) | set(task) | set(err) | set(calls) | set(search)

    agents = []
    for name in sorted(names):
        r = reg.get(name, {})
        rt = rec.get(name, {})
        tk = task.get(name, {})
        er = err.get(name, {})
        se = search.get(name, {})
        held = {
            "created": int(tk.get("created_n") or 0), "done": int(tk.get("done_n") or 0),
            "open": int(tk.get("open_n") or 0), "doing": int(tk.get("doing") or 0),
            "blocked": int(tk.get("blocked") or 0), "dead": int(tk.get("dead_n") or 0),
            "stale": int(tk.get("stale") or 0), "waiting": int(tk.get("waiting") or 0),
            "overdue": int(tk.get("overdue") or 0), "attempts": int(tk.get("attempts") or 0),
        }
        risks = []
        if held["stale"]:
            risks.append(f"租约逾期 {held['stale']}")
        if held["waiting"]:
            risks.append(f"等审批 {held['waiting']}")
        if held["overdue"]:
            risks.append(f"已逾期 {held['overdue']}")
        if held["dead"]:
            risks.append(f"死信 {held['dead']}")
        if int(er.get("open_n") or 0):
            risks.append(f"未闭错误 {int(er.get('open_n') or 0)}")
        if name not in online and held["open"]:
            risks.append("无心跳但有在办任务")
        if (r.get("control_state") or "active") != "active" and held["open"]:
            risks.append(f"控制状态 {(r.get('control_state') or '').strip()} 仍有在办")
        agents.append({
            "agent": name,
            "online": name in online,
            "control": (r.get("control_state") or "active").strip(),
            "control_note": (r.get("control_note") or "").strip(),
            "last_seen": (r.get("last_seen") or "").strip(),
            "skills": [s for s in (r.get("skills") or "").split(",") if s.strip()],
            "records": int(rt.get("n") or 0),
            "record_days": int(rt.get("days") or 0),
            "last_record": (rt.get("last") or "").strip(),
            "memories": int(mem.get(name) or 0),
            "tasks": held,
            "errors": {"open": int(er.get("open_n") or 0),
                       "opened": int(er.get("opened") or 0)},
            "usage": {"calls": int(calls.get(name) or 0),
                      "searches": int(se.get("n") or 0),
                      "zero_hit": int(se.get("zero") or 0)},
            "risks": risks,
        })

    def active(a: dict) -> bool:
        t = a["tasks"]
        return bool(a["online"] or a["records"] or a["memories"] or t["open"]
                    or t["created"] or t["done"] or a["errors"]["open"]
                    or a["usage"]["calls"])

    agents = [a for a in agents if active(a)]
    agents.sort(key=lambda a: (-a["tasks"]["open"], -a["records"], -a["usage"]["calls"], a["agent"]))

    def owner_of(row) -> str:
        return clean(row.get("owner"))

    stale = [t for t in open_rows if t["status"] == "in_progress"
             and (t.get("claimed_at") or "").strip() and t["claimed_at"] < claim_cut]
    unowned = [t for t in open_rows if not owner_of(t)]
    waiting = [t for t in open_rows if t["status"] == "in_progress"
               and int(t.get("approval_required") or 0)
               and not (t.get("approved_by") or "").strip()]
    approval_aging = [t for t in waiting
                      if (h := _age_hours((t.get("claimed_at") or "").strip(), now))
                      is not None and h >= TASK_APPROVAL_AGING_H]
    overdue = [t for t in open_rows if t["status"] == "in_progress"
               and (t.get("due") or "").strip() and t["due"][:10] < today]
    high_retry = [t for t in open_rows if t["status"] == "in_progress"
                  and int(t.get("attempts") or 0) >= 2]
    offline = [a for a in agents if not a["online"] and a["tasks"]["open"] > 0]
    wip = {
        "in_progress": sum(1 for t in open_rows if t["status"] == "in_progress"),
        "approval_waiting": len(waiting),
        "approval_aging": len(approval_aging),
        "stale_leases": len(stale),
        "unowned": len(unowned),
        "overdue": len(overdue),
    }

    def task_sample(t) -> str:
        who = f"@{owner_of(t)}" if owner_of(t) else "无主"
        return f"#{t['id']} {str(t.get('title') or '')[:24]}（{who}）"

    def add(kind: str, rows: list, hint: str) -> None:
        if not rows:
            return
        risks.append({
            "kind": kind, "count": len(rows),
            "agents": sorted({owner_of(t) for t in rows if owner_of(t)}),
            "sample": [task_sample(t) for t in rows[:3]],
            "hint": hint,
        })

    risks = []
    add("lease_stale", stale, "hub_claim 接管，或让持有者续租/收尾")
    add("unowned", unowned, "hub_claim 认领；长期无人做就 hub_task_update 关单")
    add("approval_waiting", waiting,
        "管理面审批后 hub_task_update(approved_by=...)，别让任务空等")
    add("approval_aging", approval_aging,
        f"待批已 ≥{TASK_APPROVAL_AGING_H}h：先看进展——还在施工就催进度/改期，"
        "真在等批就 hub_task_approve；只提示复核，不是硬 SLA")
    add("handoff_stale", handoff_stale,
        f"未认领交接已 ≥{HANDOFF_STALE_DAYS} 天：hub_handoff claim 接手；"
        "已废弃的请在项目记录说明后重建，别让旧交接一直挂着")
    add("overdue", overdue, "核对截止日：改期、拆分，或明确取消")
    add("dead_letter", dead_rows, "看 last_error 复盘：重开任务或确认放弃")
    add("retry_storm", high_retry, "先看 last_error，别盲目重试；需要时 hub_handoff 交接")
    if offline:
        risks.append({
            "kind": "offline_with_open", "count": len(offline),
            "agents": [a["agent"] for a in offline],
            "sample": [f"{a['agent']} 持有 {a['tasks']['open']} 条" for a in offline[:3]],
            "hint": "先 hub_heartbeat 登记；长期离线用 hub_handoff 交接或 hub_claim 接管",
        })
    err_open = [a for a in agents if a["errors"]["open"]]
    if err_open:
        risks.append({
            "kind": "errors_open", "count": sum(a["errors"]["open"] for a in err_open),
            "agents": [a["agent"] for a in err_open],
            "sample": [f"{a['agent']} 未闭 {a['errors']['open']} 条" for a in err_open[:3]],
            "hint": "修完用 hub_error_close 关闭，别留僵尸",
        })
    if zero_samples:
        risks.append({
            "kind": "zero_hit_search", "count": flow["zero_hit_searches"],
            "agents": sorted({clean(r["agent"]) for r in zero_samples if clean(r["agent"])}),
            "sample": [f"{clean(r['agent']) or '未知'}：{str(r['query'])[:24]}×{r['n']}"
                       for r in zero_samples[:3]],
            "hint": "换关键词/补记录；同词反复零命中说明索引或描述要改",
        })
    if sec["total"]:
        risks.append({
            "kind": "security_denied", "count": sec["total"], "agents": [],
            "sample": [f"{x['tool']} {x['count']} 次" for x in sec["by_tool"][:3]],
            "hint": f"近 {days} 天越权拦截 {sec['total']} 次（未锁身份 {sec['identity']} / "
                    f"角色越权 {sec['role']}）——检查 MCP 是否用 --agent 启动、"
                    'worker 是否该授 manager；hub_status(view="security") 看明细',
        })

    order = ["lease_stale", "unowned", "approval_waiting", "approval_aging", "overdue",
             "offline_with_open", "dead_letter", "retry_storm", "handoff_stale",
             "security_denied", "errors_open", "zero_hit_search"]
    risks.sort(key=lambda x: (order.index(x["kind"]) if x["kind"] in order else 99,
                              -x["count"]))
    suggestions = [r["hint"] for r in risks]
    fr = freeze_get(root)
    if fr.get("active"):
        until = str(fr.get("until") or "")[:16].replace("T", " ")
        suggestions.insert(0, f"写入冻结中（{fr.get('by') or '-'}｜{until or '手动解冻'}｜"
                              f"{str(fr.get('reason') or '')[:40]}）——"
                              "确认后 hub_freeze(action=\"unfreeze\")")
    if not suggestions:
        suggestions = ["无风险信号：保持节奏；开工先 hub_heartbeat，收工 hub_log_work。"]

    current = {
        "online_agents": len(online & set(names)),
        "known_agents": len(names),
        "by_status": by_status,
        "open_total": sum(v for k, v in by_status.items()
                          if k not in ("done", "cancelled", "dead")),
        "wip": wip,
        "handoffs": {"open": len(handoff_rows), "stale": len(handoff_stale)},
        "security": {"denied": sec["total"], "identity": sec["identity"],
                     "role": sec["role"]},
        "claim_ttl_hours": CLAIM_TTL_HOURS,
        "freeze": {"active": bool(fr.get("active")), "until": fr.get("until") or "",
                   "reason": fr.get("reason") or "", "by": fr.get("by") or ""},
    }
    return {
        "window_days": days, "since": cutoff, "now": now.isoformat(timespec="seconds"),
        "flow": flow, "current": current, "agents": agents,
        "risks": risks, "suggestions": suggestions,
        "notes": ["窗口=近 N 天；记录按 date，记忆/任务/错误/调用按 created/ts；"
                  "在办/租约/审批/未闭错误是当前快照",
                  "agent 归因按 owner/agent 字段归一（小写去空格）；空归属单列在风险里",
                  "完成周期与审批耗时口径同 hub_tasks(view=\"stats\")"],
    }


def task_closure_quality(root: str, tid) -> tuple:
    """R40 完成质量体检（只读、建议性质）：给 done 任务算「关闭得是否扎实」。
    检查项都是可观测事实，不做主观评分：过程痕迹（人工检查点/绑定记录/交付物声明）、
    交付物核验、关闭说明、极速关单。返回 (错误或 "", 报告)。
    阻断权永远在持有者——本函数只提示，不改任务状态。"""
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return f"任务 id 非法：{tid!r}", {}
    row = task_get(root, tid)
    if not row:
        return f"任务 #{tid} 不存在", {}
    if row["status"] != "done":
        return "", {"task": tid, "title": row["title"], "project": row["project"] or "",
                    "status": row["status"], "done": False, "score": None,
                    "checks": [], "warnings": [],
                    "note": f"任务当前为「{TASK_CN.get(row['status'], row['status'])}」，"
                            "完成质量体检只针对 done 任务"}
    with db_conn(root) as conn:
        cps = [dict(r) for r in conn.execute(
            "SELECT id,kind,ts,content FROM checkpoints WHERE task_id=? ORDER BY id", (tid,))]
        recs = [dict(r) for r in conn.execute(
            "SELECT id,title FROM records WHERE task_id=? AND status='active' "
            "AND record_type!='crystal' ORDER BY id", (tid,))]
    dv = (row.get("deliverables") or "").strip()
    detail = (row.get("detail") or "").strip()
    manual_cps = [c for c in cps if (c.get("kind") or "manual") not in ("auto_claim",)]
    evidence = _verify_deliverables(root, row.get("project", ""), dv, row.get("created", ""))
    checks: list = []
    warns = 0
    infos = 0

    def add(code: str, level: str, message: str) -> None:
        nonlocal warns, infos
        checks.append({"code": code, "level": level, "message": message})
        if level == "warn":
            warns += 1
        elif level == "info":
            infos += 1

    if not manual_cps and not recs and not dv:
        if cps:
            add("anchor_only", "warn",
                "只有认领自动锚点、没有过程痕迹——自动锚点只证明开过工，不证明做完")
        else:
            add("no_evidence", "warn",
                "无过程痕迹：完成时既没有人工检查点、没有绑定工作记录，也没声明交付物——"
                "下次至少留一条 hub_checkpoint 或 hub_log_work(task_id=...)")
    elif manual_cps and not recs and not dv:
        add("no_record", "info",
            "有检查点但没有绑定工作记录/交付物——经验还没落进沉淀区，"
            "建议 hub_log_work(task_id=...) 或声明 deliverables")
    if dv:
        c = evidence["counts"]
        bad = c["missing"] + c["stale"]
        if bad:
            add("deliverable_bad", "warn",
                f"交付物 {bad}/{evidence['total']} 缺失或陈旧"
                f"（missing={c['missing']} stale={c['stale']}）——"
                "确认是漏交付还是声明写错")
        else:
            add("deliverable_ok", "ok",
                f"交付物 {evidence['total']} 项均通过核验")
    if not detail:
        add("no_detail", "info",
            "没有关闭说明（detail 为空）——补一句做了什么/验证了什么，方便复盘与接手")
    secs = _seconds_between(row.get("claimed_at", ""), row.get("done_at", ""))
    if secs is not None and secs < 120 and not manual_cps:
        add("instant_close", "warn",
            f"认领后 {max(0, int(secs))} 秒即完成且无人工检查点——"
            "确认不是误点/占位任务，必要时 hub_task_update 改回 pending 补做")
    score = max(0, 100 - 30 * warns - 5 * infos)
    return "", {
        "task": tid, "title": row["title"], "project": row["project"] or "",
        "status": row["status"], "done": True, "score": score,
        "ok": warns == 0,
        "checks": checks,
        "warnings": [c["message"] for c in checks if c["level"] == "warn"],
        "stats": {"checkpoints": len(cps), "manual_checkpoints": len(manual_cps),
                  "records": len(recs), "deliverables": evidence["total"],
                  "deliverables_ok": evidence["counts"]["ok"]},
        "evidence": evidence,
    }


def task_closure_quality_report(root: str, days: int = 7, project: str = "",
                                limit: int = 20) -> dict:
    """R40 近 N 天完成任务的质检汇总（只读）：逐单体检并按分数升序（最差的先看）。"""
    try:
        days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days = 7
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)) \
        .isoformat(timespec="seconds")
    where = "status='done' AND done_at>=?"
    args: list = [cutoff]
    if (project or "").strip():
        where += " AND project=?"
        args.append(project.strip())
    with db_conn(root) as conn:
        # R276：LIMIT 前先数一遍候选——counts.total 是「体检了几单」，不是窗口内总数。
        matched = int(conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE {where}", args).fetchone()[0])
        ids = [r[0] for r in conn.execute(
            f"SELECT id FROM tasks WHERE {where} ORDER BY done_at DESC LIMIT ?",
            args + [limit])]
    items = []
    for tid in ids:
        err, rep = task_closure_quality(root, tid)
        if not err and rep.get("done"):
            items.append(rep)
    items.sort(key=lambda x: (x["score"], -x["task"]))
    scores = [x["score"] for x in items]
    return {"window_days": days, "project": (project or "").strip(),
            "matched": matched,
            "counts": {"total": len(items),
                       "ok": sum(1 for x in items if x["ok"]),
                       "warned": sum(1 for x in items if not x["ok"]),
                       "avg_score": round(sum(scores) / len(scores), 1) if scores else 0},
            "items": items}


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
               agent: str = "", kind: str = "") -> list:
    """事件流（倒序）；增量订阅传 since_id=上次最大 id 只取新事件。"""
    q = "SELECT * FROM events WHERE id>?"
    args: list = [max(0, int(since_id or 0))]
    if project:
        q += " AND project=?"
        args.append(project)
    if agent:
        q += " AND agent=?"
        args.append(_norm_agent(agent) or agent)
    if kind:
        q += " AND kind=?"
        args.append(kind)
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
    是最昂贵的错法：agent 照着过期路径干活。
    R35：日期比较统一用 localtime——全库时间戳（_now()/date.today()）都是本地时间，
    而 SQLite 的 date('now') 是 UTC：UTC+8 的凌晨会把「昨天到期」判成今天仍有效。"""
    return (f"{a}status='active' AND {a}superseded_by=0 "
            f"AND ({a}valid_until='' OR {a}valid_until>=date('now','localtime'))")


def _rec_live_sql(a: str = "") -> str:
    """records 版「现行有效」条件（P1-1）：未软删 + 未被取代 + 未失效。撤销 = status 软删 +
    valid_until 落值；被新版本取代 = superseded_by 指过去。行一律保留可追溯（双时相
    "失效≠删除"），检索/召回路径走本过滤。
    COALESCE 兜 NULL：老库/手工 UPDATE 可能留下 NULL（`NULL=0` 为假会把记录静默漏掉）。
    日期比较同 _mem_live_sql 用 localtime（R35 时区一致性）。"""
    return (f"{a}status='active' AND COALESCE({a}superseded_by,0)=0 "
            f"AND ({a}valid_until='' OR {a}valid_until>=date('now','localtime'))")


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


def supersede_record(root: str, old_id: int, new_id: int) -> str:
    """显式「#new 取代 #old」：给已存在的两条工作记录建立取代关系。返回错误或 ""。

    与记忆版同语义：旧条退出检索/时间线汇总（_rec_live_sql 过滤 superseded_by=0），
    但行、正文与双链保留——hub_get_record 仍可按 id 精读，便于追溯。
    反向调用即撤销（keep/drop 互换）。"""
    try:
        old_id, new_id = int(old_id or 0), int(new_id or 0)
    except (TypeError, ValueError):
        return "old_id / new_id 必须是正整数"
    if old_id <= 0 or new_id <= 0:
        return "old_id / new_id 必须是正整数"
    if old_id == new_id:
        return "不能自己取代自己"
    with db_conn(root) as conn:
        a = conn.execute("SELECT id, status FROM records WHERE id=?", (old_id,)).fetchone()
        b = conn.execute("SELECT id, status FROM records WHERE id=?", (new_id,)).fetchone()
        if not a or not b:
            return f"记录 #{old_id} 或 #{new_id} 不存在"
        if a["status"] != "active" or b["status"] != "active":
            return f"记录 #{old_id} 或 #{new_id} 非 active（已撤销/隔离），不能建立取代关系"
        conn.execute("UPDATE records SET superseded_by=? WHERE id=?", (new_id, old_id))
        conn.execute("UPDATE records SET superseded_by=0 WHERE id=?", (new_id,))
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
            "confidence": m["confidence"], "superseded_by": m["superseded_by"],
            "confirmed": m.get("confirmed", "")}]
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
_RRF_W_SEMANTIC = 2.0  # 记忆语义路权重：与 BM25 顶权对齐（1.5 时语义 rank1 会被 BM25 rank1 压过——实测
# 「多个 agent 同时改一个项目怎么防冲突」目标语义 rank1、词法 36/57，融合后只到 rank6；2.0 后进 top5。
# 2026-10-20 R6 冻结快照实测：MRR 0.839→0.842、Hit@5 26/28→28/28（与阈值 0.58 联动）；0=关闭独立降级到两路）
_RRF_W_SEMANTIC_RECORDS = 1.5  # 记录语义路权重刻意与记忆分开：同一快照 A/B 实测 2.0 让 records
# MRR 0.856→0.838、Hit@1 23/29→22/29（记录更长、语义召回的头部噪声更大）；阈值 0.58 对 records
# 无影响（0.58/1.5 与 0.60/1.5 逐位相同）。两表语料特性不同，不追求同一常数。
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
# 同事实多版本对内降序的相似度阈值：与 ghost 检测（similar_memories/ghost_memory_candidates）
# 同口径同值——「疑似同一事实的两版」在这套系统里已经有公认刻度，不另立一套。
_GHOST_SIM = 0.55


def _demote_older_versions(rows: list, threshold: float = 0.0) -> list:
    """R3：检索结果内**同事实多版本**的对内降序（旧版按「世代」沉底 + 标注指向现行版）。

    治什么：ghost 对（内容高度相似、又没人标 supersedes 的两版）会被一起返回，而
    BM25/LIKE 分数**不看时间**——旧版常排在较新版之前，stale_eval ③ recency 只有
    50%，模型从混合物里综合出四不像。业界给这个失效模式起了名字（llms3）：
    「记住太多版本」比「忘了」更常见。

    为什么不是全局 recency 项：P4.4 三档实测 β>0 全局掉分（0.878→0.842）——
    新旧不该全局影响排序，「同一事实的多版本谁在前」才该管。本函数**只动版本间顺序**：
    非对子行的相对位置逐一不变（这是回归里双 hub 对照死守的判据）。

    为什么是降序而不是折叠：fold 的门槛是 0.85（「几乎一模一样」）；这里是 0.55
    （「疑似同一事实」），误判代价不对称——宁可双亮相 + 标注指向，不可替用户藏起来。
    `_adjudicate` 不给置信结论（版本打平 / 质量冲突）时**不动**：那是「只标不裁」的
    既有立场（R5），系统不假装知道旧的那条就一定错。

    世代数（为什么不是简单的「沉底」）：三版链 A<B<C 时 B 既是 C 的旧版、又是 A 的
    新版——二值标记会把 A、B 一起沉底且保持原相对顺序，A 仍可能排在 B 前（链式
    不传递）。改为沿「A 旧于 B」边取**最长路径**：C 世代 0、B 世代 1、A 世代 2，
    按 (世代, 原下标) 稳定排序——同世代内仍保持原相对顺序，跨世代才重排。
    判定环理论上存在（质量 tiebreak 的石头剪刀布），迭代次数按行数封顶，不会打转。

    实现口径与 `_fold_near_dupes` 完全同源（同一 `_tokens`、同一 Jaccard、同一上界
    短路），只是阈值与动作不同——两套相似度刻度是这个库里最容易漂的东西，能复用就复用。
    """
    thr = threshold or _GHOST_SIM
    if thr <= 0 or len(rows) < 2:
        return rows
    toks = [(i, _tokens(r.get("content") or r.get("title") or "")) for i, r in enumerate(rows)]
    edges: list = []          # (旧 id, 新 id)——旧的那条输给新的一条
    for i in range(len(rows)):
        ta = toks[i][1]
        if not ta:
            continue
        for j in range(i + 1, len(rows)):
            tb = toks[j][1]
            if not tb:
                continue
            sa, sb = len(ta), len(tb)
            lo, hi = (sa, sb) if sa < sb else (sb, sa)
            if lo / hi < thr:      # 上界短路（同 similar_memories 的 B5）
                continue
            inter = len(ta & tb)
            sim = inter / (sa + sb - inter)
            if sim < thr:
                continue
            adj = _adjudicate(rows[i], rows[j], _MEM_VERS, _MEM_QUALS)
            if not adj.get("confident"):
                continue          # 只标不裁立场：不置信就不动顺序
            keep_id, drop_id = adj["keep"], adj["drop"]
            if keep_id == drop_id:
                continue
            edges.append((drop_id, keep_id))
            for k in (i, j):      # 旧的那条：标注指向现行版（行内字典可变，不动数据）
                if rows[k]["id"] == drop_id:
                    rows[k]["_older_version_of"] = keep_id
                    break
    if not edges:
        return rows
    # 世代数：沿「旧于」边的最长路径（链式 A<B<C → 0/1/2）；环由迭代封顶兜住
    gen = {r["id"]: 0 for r in rows}
    for _ in range(len(rows)):
        changed = False
        for loser, winner in edges:
            if gen[loser] < gen[winner] + 1:
                gen[loser] = gen[winner] + 1
                changed = True
        if not changed:
            break
    # 稳定排序：世代升序，同世代保持原相对顺序（Python sort 稳定，这是选它的理由）
    order = sorted(range(len(rows)), key=lambda k: (gen[rows[k]["id"]], k))
    return [rows[k] for k in order]


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
            # R106（任务 #13）：冲突对补「谁现行」确定性标注（A-TMA：给检索证据打
            # current/historical 标签，冲突解析准确率显著提升——模型从混合物里综合
            # 是真实失效面）。复用 D5 _adjudicate 的分层规则；仅 confident=True 才打
            # 标（版本信号打平时系统不知道真相，保持只标互斥）。不改排序、不改数据：
            # 词法分高者仍可排前（相关性公平竞争），但消费方能看见谁新谁旧。
            ra, rb = rows[i], rows[j]
            if "created" in ra and "created" in rb and "updated" not in ra:
                vers, quals = _REC_VERS, _REC_QUALS
            else:
                vers, quals = _MEM_VERS, _MEM_QUALS
            try:
                sugg = _adjudicate(ra, rb, vers, quals)
            except Exception:  # noqa: BLE001 裁决失败退回只标互斥，不拖垮检索
                continue
            if not sugg.get("confident"):
                continue
            keep, drop = sugg["keep"], sugg["drop"]
            newer = ra if keep == ra.get("id") else rb
            older = rb if newer is ra else ra
            newer.setdefault("_current_hint", []).append(older["id"])
            older.setdefault("_superseded_hint", []).append(newer["id"])
    return rows


def _fold_near_dupes(rows: list, threshold: float = 0.0) -> list:
    """检索结果内近重复折叠：同一件事的多条只保留一条。

    为什么需要：库里必然有近似重复（M7 整合提案就是实证），一起返回时同一个事实在上下文里
    占好几个位置，既费 token 又把真相关的条目挤出 limit。业界混合检索流水线把它列为
    **固定的第 3 步**（RRF 之后、进上下文之前）：dedup before context。

    折叠=不返回，但在保留行上留 `_dup_n`（折了几条）——信息不丢，事后可查。
    阈值取高（0.85）：只折"几乎一模一样"的。宁可漏折，不可误折——
    误折会让 agent 少看到一条真不同的事实，这比多看一条重复贵得多。

    R3 增量（版本感知留存）：折的是**同一事实的两版**时，留**新的**那版，不是留分数高的。
    原实现按融合分留——而近重复文本对同一查询的词法命中几乎一致，分数先后基本任意，
    实测把旧版当现行版留在上下文里（stale_eval「同步间隔」对：sim=0.88 被折叠，
    留下的竟是 30 天前的旧值）。版本信号不可分辨时（同秒双写、质量全同）维持原行为，
    与 _demote_older_versions 的「不置信不动」是同一条纪律。保留行带 `_folded_older`
    （被折掉的旧版 id）——折叠也要可追溯，不能只剩一个计数。"""
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
            k = kept[hit]
            dup_n = int(k.get("_dup_n") or 0) + 1
            adj = (_adjudicate(r, k, _MEM_VERS, _MEM_QUALS)
                   if (r.get("id") and k.get("id")) else {})
            if adj.get("confident") and adj.get("drop") == k.get("id"):
                # 留新退旧：新行顶替留存位（_dup_n 结转——折掉的都是同一事实的其余版本）
                r["_dup_n"] = dup_n
                r["_folded_older"] = k.get("id")
                kept[hit] = r
                kept_toks[hit] = t
            else:
                k["_dup_n"] = dup_n
            continue
        kept.append(r)
        kept_toks.append(t)
    return kept


_GRAPH_SQL_CHUNK = 400


def _graph_edges_touching(conn, nodes: set, want: str = "") -> list:
    """只取与 nodes 相连的边（按边 id 升序）；want 非空时只留对端为 want 类型的边。

    与 _graph_adj 的全表扫描相比，走 UNIQUE(from_kind,from_id,…) / idx_rel_to 逐节点
    取边，读的行数从「整张 relations」降到「这几层的边」（两列都在索引里，覆盖读）。
    from_id 按整数绑定：schema 是 INTEGER，且三个写入方（graph_build / sync_links /
    relation_add）都 int() 归一——真实库 2725 行 typeof(from_id) 全为 integer。
    to_ref 按字符串绑定（tag/project 名本就是文本），'7' 这类由列亲和性折成 7 走索引。
    """
    by_kind: dict = {}
    for kind, ref in nodes:
        by_kind.setdefault(kind, set()).add(str(ref))
    found: dict = {}
    for kind, refs in by_kind.items():
        fids = sorted({int(x) for x in refs if x.lstrip("-").isdigit()})
        trefs = sorted(refs)
        for i in range(0, max(len(fids), len(trefs)), _GRAPH_SQL_CHUNK):
            fpart = fids[i:i + _GRAPH_SQL_CHUNK]
            if fpart:
                sql = ("SELECT id,from_kind,from_id,to_kind,to_ref,kind FROM relations "
                       "WHERE from_kind=? AND from_id IN (" + ",".join("?" * len(fpart)) + ")")
                args: list = [kind, *fpart]
                if want:
                    sql += " AND to_kind=?"
                    args.append(want)
                for e in conn.execute(sql, args):
                    found[e["id"]] = e
            tpart = trefs[i:i + _GRAPH_SQL_CHUNK]
            if tpart:
                sql = ("SELECT id,from_kind,from_id,to_kind,to_ref,kind FROM relations "
                       "WHERE to_kind=? AND to_ref IN (" + ",".join("?" * len(tpart)) + ")")
                args = [kind, *tpart]
                if want:
                    sql += " AND from_kind=?"
                    args.append(want)
                for e in conn.execute(sql, args):
                    found[e["id"]] = e
    return [found[k] for k in sorted(found)]


def _graph_local_adj(conn, seeds: set, want: str) -> dict:
    """图扩展真正会读到的局部邻接表（种子 + 1.._GRAPH_HOPS-1 层节点的边）。

    等价性依据（R249：真实库 98 个种子集与全表 _graph_adj 逐位比对 0 差异）：
      · 遍历只读 adj[n]（n 在前 _GRAPH_HOPS-1 层内），这里把这些层的边全取来；
      · 每节点邻居按边 id 升序追加，与 _graph_adj 全表扫描的 rowid 序逐项一致，
        分数累加顺序相同 → 分数与截断排序可位级对齐；
      · 最后一层只留对端为 want 类型的边：这层不再向外走，非目标类型的邻居只进 seen
        不计分，去掉它们不改变任何目标节点的「首次命中」顺序与分数。
    """
    adj: dict = {}
    fetched: set = set()
    visited = set(seeds)
    frontier = set(seeds)
    for hop in range(_GRAPH_HOPS):
        if not frontier:
            break
        nxt = set()
        rows = _graph_edges_touching(conn, frontier,
                                     want if hop == _GRAPH_HOPS - 1 else "")
        for e in rows:
            if e["id"] in fetched:
                continue
            fetched.add(e["id"])
            a = (e["from_kind"], str(e["from_id"]))
            b = (e["to_kind"], str(e["to_ref"]))
            adj.setdefault(a, []).append((b, e["kind"]))
            adj.setdefault(b, []).append((a, e["kind"]))
        for n in frontier:
            for nb, _ek in adj.get(n, []):
                if nb not in visited:
                    visited.add(nb)
                    nxt.add(nb)
        frontier = nxt
    return adj


def _graph_expand(root: str, entity: str, seeds: list, limit: int = 30) -> list:
    """图扩展召回：从种子做 1~2 跳遍历，返回 [(eid, score)]（分数越高越「图上近」）。

    entity: "record" / "memory"（与 relations 表的 kind 口径一致）。
    计分：2.0/跳数 + 显式边（双链 mention / 手工 link）0.5 加成——
    显式边说明「有人认定它们有关」，比结构边（同项目/同标签）更能说明问题。
    取边走 _graph_local_adj（R249）：以前每次检索都全表重建邻接表 ~7.8ms，
    现在只取遍历会读到的两层 ~1.9-2.7ms，结果逐位不变。
    """
    if not seeds or _RRF_W_GRAPH <= 0:
        return []
    seeds = {(entity, str(s)) for s in seeds}
    try:
        with db_conn(root) as conn:
            adj = _graph_local_adj(conn, seeds, entity)
    except sqlite3.Error:
        return []
    scores: dict = {}
    for seed in seeds:
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
    seeds = {(src, str(s)) for s in seeds}
    try:
        with db_conn(root) as conn:
            adj = _graph_local_adj(conn, seeds, dst)
    except sqlite3.Error:
        return []
    scores: dict = {}
    for seed in seeds:
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
        dst_rows = r["memories"] if dst == "memory" else r["records"]
        have = {x["id"] for x in dst_rows}
        pending = [eid for eid, _sc in _graph_expand_cross(root, src, dst, seeds, limit=8)
                   if eid not in have]
        if not pending:
            continue
        # 一次 IN 查询取齐（R298）：原实现每个候选单开连接，实测一次 search_all 里有
        # 16/37 个连接耗在这——批量后每方向 1 个，结果与顺序不变。
        fetched = _rows_by_ids(root, dst, pending)
        for eid in pending:
            row = fetched.get(eid)
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
_ST_IMPORTED = None  # None=未尝试 / True=可用 / False=不可用
_ST_CLS = None
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
# ③ 余弦 ≥ 0.58——2026-10-20 R6 在真实库 30 查询评测集上重标定：真相关 #44「写记录时自动踩坑拦截」
#    余弦 0.5852 被 0.60 挡在门外（BM25 rank6 也救不回，覆盖度闸门把它当弱候选丢掉）；
#    0.55 放入 52 条候选、MRR 0.839→0.819（噪声回潮），0.58 只放入 19 条且那条进 top5、
#    无 top5 回归。仍是「剪噪声底」而非分离器——不要再用单个阈值追求语义精度。
_SEM_MIN_SIM = 0.58
_SEM_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _st_cls():
    """懒加载 sentence_transformers（首次真正走语义路时才 import）。

    R217：原先在模块顶层 import——本机装了它就等于 GUI/MCP 启动连带拉
    transformers+torch（实测启动多 2–5s、RSS 多几百 MB），对不用语义路的
    调用纯属惩罚。改为用时再 import；失败一次即缓存失败，仍 fail-open。"""
    global _ST_IMPORTED, _ST_CLS
    if _ST_IMPORTED is None:
        try:
            from sentence_transformers import SentenceTransformer
            _ST_CLS = SentenceTransformer
            _ST_IMPORTED = True
        except Exception:  # noqa: BLE001 可选依赖缺失/装坏都不得影响主功能
            _ST_IMPORTED = False
    return _ST_CLS


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
        cls = _st_cls()
        if cls is None:
            _EMB_READY = False
            return False
        try:
            try:
                # 本地缓存优先（2026-10-11 线上实测教训）：网络不可达且代理关闭时，HF 联网检查
                # 会重试 ~5 分钟（实测 304s）最终失败 → fail-open 变成"语义路永久静默报废"。
                # 缓存已在 → local_files_only 纯本地秒载；缓存缺失 → 抛错后走联网下载（首次装配）。
                _EMB = cls(_EMB_MODEL_NAME, local_files_only=True)
            except Exception:
                _EMB = cls(_EMB_MODEL_NAME)
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

# R257：语义模型的**后台预加载**（读路径不等它）
_EMB_BG_THREAD = None
_EMB_BG_LOCK = threading.Lock()


def _emb_ready_now() -> bool:
    """读路径专用：不等待模型加载，未就绪就起后台线程慢慢装。

    实测（`output/_r257_slow_query.txt`）：`_ensure_emb()` 首次调用 **9190ms**（torch +
    transformers 导入 + 权重加载），而单次检索 p50 只 74.5ms —— 相差 123 倍。表现就是
    「GUI 搜索框里第一次输入中文，卡 9 秒」，用户感知是死机而不是「第一次稍慢」。
    语义路本来就是可选第三路、全程 fail-open，不该让可选能力锁住主链路：这里起一个
    daemon 线程去装，本次直接返回 False（调用方走词法两路 + 图路，结果依然可用），
    装完 `_EMB_READY=True`，下一次检索自动带上语义路。

    **写路径仍然走阻塞版 `_ensure_emb()`**（add_memory/add_record/改正文）：向量必须
    当场落库，否则新写入的数据在语义路上长期缺席，比慢 9 秒更糟。
    """
    global _EMB_BG_THREAD
    if _EMB_READY is not None:
        return _EMB_READY
    with _EMB_BG_LOCK:
        if _EMB_BG_THREAD is None:
            t = threading.Thread(target=_ensure_emb, name="agenthub-emb-warm", daemon=True)
            _EMB_BG_THREAD = t
            t.start()
    return False

# ── R252（v2.41.111）：向量矩阵进程内缓存 ──────────────────────────────────
# R251 把「一次检索拉回 1576 行带 content 的 r.*」收成「窄取 eid+vec」，但窄取本身仍是
# 每次 13.9ms（3.2MB BLOB + JOIN 存活谓词 + 1576 个 Row 对象），余弦只 1.3ms ——
# 同一进程连续检索读到的是同一批向量，把它缓存成矩阵就能把这段变成 0 次查询。
# 失效判据用**库文件指纹**（brain.db 与 brain.db-wal 的 (mtime_ns, size)）：
# 本库所有写入都过 WAL，任何跨进程提交必然改变 -wal 的 size/mtime；checkpoint 会同时
# 动 db 文件 → 只会「多失效一次」（重查一次，无害），不会「该失效却没失效」。
# 命中多付两次 os.stat（实测 ~0.03ms），比省下的 1 次 COUNT(*)（1.1ms）还便宜一个量级。
# 只缓存非空结果；空表仍走 COUNT 短路（表被清空时缓存自然随指纹失效）。
_SEM_MAT_CACHE: dict = {}     # (root, entity) -> (fp, kept_ids, M, dim, row_norms)
_SEM_MAT_CACHE_MAX_BYTES = 64 * 1024 * 1024   # 单库矩阵上限（512 维 ≈ 6.5 万条）


def _db_file_fp(root: str) -> tuple:
    """库文件指纹：db 与 -wal 的 (mtime_ns, size)。任一缺失记 None（不抛）。"""
    p = str(db_path(root))
    out = []
    for suffix in ("", "-wal"):
        try:
            st = os.stat(p + suffix)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


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
    if _st_cls() is None:
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
    普通记忆行读 use_count/last_hit；被取代/过期/软删的行一概不得借语义路复活。
    R251：先窄取 `e.eid, e.vec` 算余弦，只对过阈值候选（真实库每次 7–11 条）回表取全列——
    原实现把 1575 行带 content 的 `r.*` 与向量一起拉回、99% 过不了 0.58 又丢掉（实测 22.9→12.7ms，
    另省 1575 行 dict 构建），余弦本身只要 0.15ms。回表仍带存活谓词，两查之间被取代的行只会消失。
    R252：窄取结果按库文件指纹缓存成矩阵（见 _SEM_MAT_CACHE）——稳态命中时只剩
    余弦 + 回表，把 14.6ms 里最大的 13.9ms 抹掉；指纹变化（任何跨进程写入/checkpoint）
    即重建，语义与「每次现查」等价。"""
    if not query or not _SEM_CJK_RE.search(query):
        return []
    try:
        import numpy as np
    except Exception:
        return []
    key = (root, entity)
    fp = _db_file_fp(root)
    cached = _SEM_MAT_CACHE.get(key)
    if cached is not None and cached[0] != fp:
        _SEM_MAT_CACHE.pop(key, None)     # 库变了：旧矩阵立刻作废，不参与本轮
        cached = None
    if cached is None:                    # 有缓存说明表非空，省掉每次 1.1ms 的 COUNT(*)
        try:
            with db_conn(root) as c:
                if c.execute("SELECT COUNT(*) FROM embeddings WHERE entity=?", (entity,)).fetchone()[0] == 0:
                    return []   # 空表短路：无索引时不加载模型（避免无谓开销）
        except Exception:
            return []
    if not _emb_ready_now():              # R257：读路径不等 9.2s 的模型加载
        return []
    qvec = _encode([query])
    if qvec is None:
        return []
    tbl = "records" if entity == "records" else "memories"
    live = _rec_live_sql("r.") if entity == "records" else _live_sql("r.")
    live_plain = _rec_live_sql() if entity == "records" else _live_sql()
    try:
        q = qvec[0].astype("float32")
        qn = np.linalg.norm(q) + 1e-9
        if cached is not None and cached[3] == q.shape[0]:
            kept, M, norms = cached[1], cached[2], cached[4]   # 命中：0 次查询
        else:
            with db_conn(root) as c:
                rows = c.execute(
                    f"SELECT e.eid, e.vec FROM embeddings e JOIN {tbl} r ON e.eid=r.id "
                    f"WHERE e.entity=? AND {live}", (entity,)).fetchall()
            if not rows:
                return []
            # 批量矩阵点积：把所有 vec 堆成 (N,D) 矩阵，一次 (M@q) 替代逐行 np.dot + 逐行 norm
            # （原 O(N) Python 循环 → 单次 BLAS；search_all 对 records+memories 各跑一遍，放大为 2×O(N)）。
            vecs, kept = [], []
            for row in rows:
                v = np.frombuffer(row["vec"], dtype="float32")
                if v.shape[0] != q.shape[0]:
                    continue
                vecs.append(v); kept.append(row["eid"])
            if not vecs:
                return []
            M = np.stack(vecs)                                # (N, D)
            norms = np.linalg.norm(M, axis=1) + 1e-9
            if M.nbytes <= _SEM_MAT_CACHE_MAX_BYTES:          # 只在可控内存内缓存
                _SEM_MAT_CACHE[key] = (fp, kept, M, q.shape[0], norms)
        sims = (M @ q) / (qn * norms)                        # 向量化余弦，与逐行公式数学等价
        scored = [(eid, float(sim)) for eid, sim in zip(kept, sims)
                  if sim >= _SEM_MIN_SIM]                    # 门槛：弱相关不进融合（噪声治理）
        scored.sort(key=lambda t: -t[1])
        top = scored[:limit]
        if not top:
            return []
        ids = [eid for eid, _ in top]
        with db_conn(root) as c:
            full = {r["id"]: dict(r) for r in c.execute(
                f"SELECT * FROM {tbl} WHERE " + live_plain
                + " AND id IN (" + ",".join("?" * len(ids)) + ")", ids)}
        out = []
        for rank, (eid, sim) in enumerate(top):
            r = full.get(eid)
            if r is None:   # 两查之间被取代/过期：只会消失，不会复活（同 R250 回表语义）
                continue
            r["_semantic_sim"] = round(sim, 4)
            out.append((eid, 1.0 / (_RRF_K + rank + 1), r))
        return out
    except Exception:
        return []


def _memories_search_fused(root: str, query: str, words: list, kind: str, limit: int,
                           semantic: bool = True) -> list:
    """P4.3 双路融合：LIKE/计分路（_memories_query）+ BM25 路（_bm25_rank）各自 rank →
    RRF(f)=Σ1/(k+rank) 融合（rank 天然免归一化，这是选 RRF 的理由）。
    pinned 不参与检索排序（评测三轮数据定案：硬前置 0.387 / 等权通道 0.449 / 纯相关 0.905
    ——pinned 弱相关条目会压过强相关结果；其曝光职责在心跳推送/hub_status/置顶徽章）。
    融合分乘 (1+β·recency)（P4.4）：同为命中时最近用到的排前；_channels 记录的是各路
    融合前 rank（boost 属融合后调整，分层可解释）。结果带 _channels={路: rank} 可解释（铁律 3）。
    两路全零命中返回 []——bigram 重试由调用方按原语义触发。
    semantic=False 跳过语义第三路（R230）：体检泄漏抽查用——不为一次抽查加载编码模型；
    语义路的过滤器覆盖由调用方以结构性计数单独守。"""
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
    sem_rows = _semantic_rank(root, query, "memories") if semantic and _RRF_W_SEMANTIC > 0 else []  # P0-3 语义第三路
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


def list_pinned_memories(root: str, limit: int = 8) -> list:
    """置顶记忆清单（新→旧）。GUI 大脑页专用取数：此前走 `search_memories("", "", 100)`
    再筛 pinned——空查询也会进融合检索（加载语义模型），真实库 140ms 只为找 8 条置顶。
    与旧路径的可见差异只有一个方向：旧路径先截断「最近 100 条记忆」再筛，比第 100 条
    更旧的置顶会被漏掉；这里直接按 pinned 取，漏置顶不再是设计行为。"""
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, kind, content, pinned, use_count FROM memories WHERE "
            + _live_sql() + " AND pinned=1 ORDER BY id DESC LIMIT ?", (limit,))]


def search_memories(root: str, query: str = "", kind: str = "", limit: int = 20,
                    count_hits: bool = True, semantic: bool = True,
                    gate: bool = True) -> list:
    """检索记忆：多词评分（命中词数，与 records 检索一致）；无 query 时置顶优先按 id 倒序。
    0 命中且 query 含 ≥4 字连续中文串时按 2-gram 重试（与 search_records 同因）。

    count_hits：带 query 的实际检索命中才计唤起（use_count+1）——主动检索是最高价值的
    唤起信号，此前只统计被动推送，衰减/有用率的输入数据系统性失真（0.2 修复）。
    空 query 列清单不计；写入查重等内部调用须显式传 False。
    semantic=False：跳过语义第三路（R230 体检探针专用，避免为抽查加载编码模型）。
    gate=False：跳过覆盖度闸门（R245 体检泄漏探针专用）。闸门只把候选分强弱再**删**，
    删得越多漏报越多；探针要的是「管线能不能吐出已取代行」的超集，跳过闸门只会更严。
    结构性过滤（_live_sql）不归闸门管，gate=False 也不放行已取代行。默认 True 不动。"""
    words = [w for w in re.split(r"\s+", (query or "").strip()) if w][:8]
    if _RRF_ENABLED:
        rows = _memories_search_fused(root, query or "", words, kind, limit, semantic)
    else:
        rows = _memories_query(root, words, kind, limit)  # 回滚开关：走旧单路
    if gate:
        rows = _coverage_gate(root, rows, query or "")
    if not rows and words:
        retry = _retry_bigrams(query)
        if retry:
            rows = _memories_query(root, retry, kind, limit)  # 放宽重试保持旧路径（融合外）
            if gate:
                rows = _coverage_gate(root, rows, query or "")
            for r in rows:  # 同 search_records：放宽召回须可识别
                r["_bigram"] = True
    if rows:
        rows = _fold_near_dupes(rows)   # 折叠在计唤起之前：被折掉的没进上下文，不该算唤起
        rows = _mark_conflicts(rows)    # 矛盾标记（业界：取回后、进上下文前检测，别让 LLM 自己发现）
        rows = _demote_older_versions(rows)  # R3：同事实多版本对内降序（旧版沉底+标注，只动对内顺序）
        now0 = datetime.datetime.now()  # R4：新鲜度读时标注（一次 now，行间一致）
        for r in rows:
            fr = memory_freshness(r, now0)
            r["_freshness"] = fr["state"]
            r["_age_days"] = fr["age_days"]
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


def memory_freshness(row: dict, now: "datetime.datetime | None" = None) -> dict:
    """R4：单条记忆的新鲜度状态——**读时计算，无清扫进程，过期不删除**。

    外部模式（「Facts Should Expire」）：事实带 observed_at/checked_at，状态
    fresh/stale/expired 在读时算出；过期事实仍是证据只是更弱。本库的字段映射：
      observed = max(created, updated)   —— 值最后被（重）写的时点
      checked  = max(confirmed, last_hit)—— 最后被「确认还有效」的时点；
                 显式确认列（R5 hub_memory_confirm 写入）优先，没有时退回
                 last_hit（被检索≈最近有人看过——弱确认，口径在 basis 里写明）
      reference = max(observed, checked)—— 以两者较新者计龄（写过或用得近都算新）
    硬过期（valid_until 已过）优先于年龄判定——那是写入者显式划的线。

    返回 {state, age_days, basis, fresh_days, expire_days}。时间戳缺失/畸形 →
    state="unknown"（不猜：把没时间戳的当成 stale 会误报一片）。
    """
    now = now or datetime.datetime.now()
    kind = (row.get("kind") or "note")
    fresh_d, expire_d = FRESH_POLICY.get(kind, FRESH_POLICY["note"])
    base = {"state": "unknown", "age_days": None, "basis": "", "kind": kind,
            "fresh_days": fresh_d, "expire_days": expire_d}
    observed = max(str(row.get("created") or ""), str(row.get("updated") or ""))
    checked = max(str(row.get("confirmed") or ""), str(row.get("last_hit") or ""))
    reference = max(observed, checked)
    try:
        ref_dt = datetime.datetime.fromisoformat(reference)
    except (ValueError, TypeError):
        return base                      # 无可用时间戳：不猜
    age = (now - ref_dt).total_seconds() / 86400
    base["age_days"] = round(age, 1)
    base["basis"] = ("confirmed" if checked > observed else "written") \
        if checked else "written"
    vu = str(row.get("valid_until") or "").strip()
    if vu and vu < now.date().isoformat():
        base["state"] = "expired"
        base["basis"] = "valid_until"
        return base
    base["state"] = "expired" if age > expire_d else ("stale" if age > fresh_d else "fresh")
    return base


def confirm_memories(root: str, ids=None, kind: str = "", all_stale: bool = False,
                     limit: int = 50, now: str = "") -> dict:
    """复核记忆（第五轮 R2）：把「现在还成立」写进 confirmed，新鲜度读时立即重置。

    两种入口：ids=[...] 显式指定；all_stale=True 批量复核当前 stale/expired（可按
    kind 过滤，limit 截断并如实上报 remaining）。**只写 confirmed**——不碰
    content/updated/use_count：updated 表示「值最后被修改」，confirmed 表示「最后被
    确认仍成立」，混用会污染版本裁决与「谁更新」的判断。
    valid_until 已过的硬过期不确认解除（写入者显式划的线，要改就改 valid_until）。
    返回 {confirmed:[id], skipped:[{id,reason}], remaining, before:{id:state}, stamp}。
    """
    stamp = str(now or _now()).strip()
    try:
        now_dt = datetime.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        stamp, now_dt = _now(), datetime.datetime.now()
    try:
        limit = max(1, min(int(limit or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    out = {"confirmed": [], "skipped": [], "remaining": 0, "before": {}, "stamp": stamp}

    def _skip(mid, reason):
        out["skipped"].append({"id": mid, "reason": reason})

    with db_conn(root) as conn:
        if ids:
            seen = []
            for raw in ids:
                try:
                    mid = int(raw)
                except (TypeError, ValueError):
                    _skip(raw, "不是整数 id")
                    continue
                if mid not in seen:
                    seen.append(mid)
            for mid in seen:
                row = conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
                if not row:
                    _skip(mid, "不存在")
                    continue
                row = dict(row)
                if (row.get("status") or "active") != "active":
                    _skip(mid, f"非现行（status={row.get('status')}）")
                    continue
                sup = int(row.get("superseded_by") or 0)
                if sup > 0:
                    _skip(mid, f"已被 #{sup} 取代")
                    continue
                if sup < 0:
                    _skip(mid, "已作废（无继任）")
                    continue
                fr = memory_freshness(row, now_dt)
                if fr["basis"] == "valid_until":
                    _skip(mid, "valid_until 已过（硬过期；确认不能解除，请更新有效期）")
                    continue
                out["before"][mid] = fr["state"]
                conn.execute("UPDATE memories SET confirmed=? WHERE id=?", (stamp, mid))
                out["confirmed"].append(mid)
        elif all_stale:
            q = ("SELECT * FROM memories WHERE status='active' AND superseded_by=0"
                 + (" AND kind=?" if kind in KINDS else "") + " ORDER BY id")
            rows = [dict(r) for r in conn.execute(q, (kind,) if kind in KINDS else ())]
            for row in rows:
                fr = memory_freshness(row, now_dt)
                if fr["basis"] == "valid_until":
                    _skip(row["id"], "valid_until 已过（硬过期；确认不能解除，请更新有效期）")
                    continue
                if fr["state"] not in ("stale", "expired"):
                    continue
                if len(out["confirmed"]) >= limit:
                    out["remaining"] += 1
                    continue
                out["before"][row["id"]] = fr["state"]
                conn.execute("UPDATE memories SET confirmed=? WHERE id=?", (stamp, row["id"]))
                out["confirmed"].append(row["id"])
    return out


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
            # 文本改了，BM25 与相似度两个命名空间的 token 缓存必须一起失效（否则命中旧文本）——
            # 统一缓存层的单一失效入口，改这一处即两个命名空间都作废（不会漏一个）
            _TOK_CACHE.invalidate(root, mid)
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
            # R244：回滚改回的是旧正文，分词缓存里留着的是回滚前的词集——不失效的话
            # 相似度与 BM25 两个命名空间会拿着已被撤掉的文本继续算（edit_memory 一直有这一步，回滚漏了）
            _TOK_CACHE.invalidate(root, mid)
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


def project_count(root: str) -> int:
    """项目总数（R265）：list_projects 有 LIMIT，渲染不能把「取到的条数」当总数——
    R260 的 get_project 同型缺陷换了个位置：超过上限时用户会以为「就这么多」。"""
    with db_conn(root) as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] or 0)


def get_project(root: str, name: str, limit: int = 5) -> dict | None:
    """项目概况 + 最近 limit 条记录。

    R260：加 `total`（活跃记录总数）与 `shown`（本次列了几条）。此前只回 5 条、
    **不报总数**——真实项目有 359 条记录时，调用方看到 5 条会以为这就是全部，
    也不会想到还有 354 条要另找入口。信息缺失比信息过载更难发现。
    """
    with db_conn(root) as conn:
        proj = conn.execute("SELECT name, agent, created FROM projects WHERE name=?", (name,)).fetchone()
        if not proj:
            return None
        recs = [dict(r) for r in conn.execute(
            "SELECT date,agent,title,content FROM records WHERE project=? AND status='active' "
            "ORDER BY date DESC, id DESC LIMIT ?", (name, max(1, min(limit, 100))))]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM records WHERE project=? AND status='active'",
            (name,)).fetchone()["n"]
        return {"name": proj["name"], "agent": proj["agent"], "created": proj["created"],
                "last_active": conn.execute(
                    "SELECT MAX(date) AS d FROM records WHERE project=? AND status='active'",
                    (name,)).fetchone()["d"] or "",
                "records": recs, "total": total, "shown": len(recs)}


def project_exists(root: str, name: str) -> bool:
    with db_conn(root) as conn:
        return conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone() is not None


def project_names(root: str) -> list[str]:
    """全部已登记项目名（防拼错提示用，只取名字不做统计）。"""
    with db_conn(root) as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM projects")]


# ---------------------------------------------------------------- 检索与进度

def get_progress(root: str, limit: int = 30) -> list:
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT date,project,agent,title FROM records WHERE status='active' "
            "ORDER BY date DESC, id DESC LIMIT ?", (max(1, min(limit, 200)),))]


# ---------------------------------------------------------------- 会话简报（R811）

BRIEF_MAX_CHARS = 3600


def _brief_oneline(text: str, limit: int) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[: max(1, limit - 1)] + "…"


def _brief_title(text: str) -> str:
    """记录标题去掉「2026-10-30（qoder）· 」前缀——日期与作者已在行尾列出，重复只占预算。"""
    raw = (text or "").strip()
    return re.sub(r"^\d{4}-\d{2}-\d{2}(?:（[^）]*）)?(?:\s*[·:：\-]\s*|\s*$)", "", raw)


def infer_project_from_cwd(root: str, cwd: str) -> str:
    """从工作目录推断项目：目录位于大脑根下、首段是已登记项目时才认，其余返回 ""。

    只认已登记项目——路径里随手一段目录名不等于项目，猜错会让简报把别人的
    记忆推到当前会话（比不给更糟）。
    """
    if not root or not cwd:
        return ""
    try:
        rel = Path(cwd).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return ""
    if not rel.parts:
        return ""
    name = rel.parts[0]
    if name.startswith(("_", ".")) or name in ("00_Inbox", "99_Archive"):
        return ""
    try:
        return name if project_exists(root, name) else ""
    except sqlite3.Error:
        return ""


# ---------------------------------------------------------------- 即时回忆（R820）

RECALL_MAX_CHARS = 900
RECALL_TERM_CAP = 8
RECALL_STATE_TTL_H = 24
# 短句里最常见的虚词/套话。留着会把「提醒」这类常见词当无关命中，稀释真正相关的记忆。
RECALL_STOP_TERMS = {
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "如何", "可以", "需要",
    "现在", "还是", "就是", "因为", "所以", "但是", "已经", "没有", "一个", "不是",
    "然后", "如果", "以及", "这些", "那些", "帮我", "一下", "看看", "是否", "进行",
    "使用", "东西", "事情", "时候", "地方", "问题", "情况", "继续", "好的", "谢谢",
    "收到", "明白", "还是", "要不", "有点", "比较", "非常", "真的", "应该", "可能",
    # R825：实测漏网。journal#8421（2026-10-31 用户原句「围绕刚完成 agenthub 目标
    # 整体使用体验，重点…不用我提醒」）抽出 10 个词，其中「围绕/重点/整体/不用」
    # 四个纯虚词，直接把 #50/#9/#286 三条泛化元记忆顶上 top3——召回在跑，召回的
    # 不是用户问的那件事。闭集补词永远追不完，真正的解法是下面的池内区分度重排
    # （_recall_idf），这里只补最常见的口语壳。
    "不用", "不要", "不能", "不会", "围绕", "重点", "整体", "方面", "直接",
    "目前", "当前", "具体", "相关", "主要", "一次", "一点", "一样", "内容",
    "请问", "麻烦", "帮忙", "有没有", "是不是", "能不能", "对不对", "怎么样",
}
RECALL_ASCII_STOP = {
    "the", "and", "for", "with", "this", "that", "you", "are", "was", "can", "please",
    "from", "have", "has", "not", "but", "all", "any", "how", "what", "why", "when",
    "will", "would", "should", "could", "into", "about", "there", "their", "them",
}
# 长中文串在这些字/词处断开：既保住「失败分支」「退出码」这类短语，又避免
# 「支的」「的退」这种跨词 bigram 混进查询（R820 实测会把真正相关的教训挤下去）。
# R825：切分点直接由 RECALL_STOP_TERMS 生成——原先只硬编码十来个，改词表时两处
# 要手动同步，实测「围绕刚完成」原样进了检索式（停用词表里有「围绕」，但它只是
# 一条 5 字 chunk 的一部分，永远命中不了 add() 里的相等判断）。合成一处之后，
# 加词就自动变成切分点。
_STOP_ALT = "|".join(sorted(RECALL_STOP_TERMS, key=len, reverse=True))
RECALL_CHUNK_SPLIT = re.compile(
    r"(?:" + _STOP_ALT + r"|[的了是在和与就也都把被给对从到为等我你他这那吗呢吧啊呀么])")
# 意图权重：问「怎么修」时教训比泛化偏好有用；问「要不要/偏好」时反过来。
RECALL_INTENT_WORDS = {
    "howto": ("怎么", "如何", "为什么", "报错", "错误", "失败", "修复", "解决", "实现",
              "原理", "方法", "步骤", "排查", "崩溃", "异常", "怎么修", "为什么"),
    "pref": ("喜欢", "偏好", "要不要", "该不该", "以后", "记得", "习惯", "风格", "口味",
             "可不可以", "行不行"),
    "status": ("进度", "做到", "上次", "最近", "现在", "怎么样", "状态", "完成", "做到哪"),
}
RECALL_KIND_WEIGHTS = {
    "howto": {"lesson": 1.6, "fact": 1.2, "project": 1.0, "note": 0.4, "preference": 0.2},
    "pref": {"preference": 2.0, "fact": 1.0, "lesson": 0.6, "project": 0.4, "note": 0.6},
    "status": {"project": 1.5, "fact": 1.0, "lesson": 0.9, "preference": 0.5, "note": 0.5},
    "general": {"preference": 1.0, "lesson": 1.0, "fact": 0.9, "project": 0.8, "note": 0.4},
}


def _recall_intent(prompt: str) -> str:
    low = (prompt or "").lower()
    for name in ("howto", "pref", "status"):
        if any(w in low for w in RECALL_INTENT_WORDS[name]):
            return name
    return "general"


def _recall_state_file(state_path: str = "") -> Path:
    return (Path(state_path) if state_path
            else Path.home() / ".workbuddy" / "state" / "brain-recall.json")


def _load_recall_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"v": 1, "sessions": {}}


def _save_recall_state(path: Path, state: dict, now: "datetime.datetime | None" = None) -> None:
    """按会话记「已经送过哪些记忆」，避免同一会话反复注入同一条。

    只做尽力而为：写失败不影响本次召回（钩子 fail-open 优先）。
    """
    now = now or datetime.datetime.now()
    cutoff = now - datetime.timedelta(hours=RECALL_STATE_TTL_H)
    kept = {}
    for key, val in (state.get("sessions") or {}).items():
        try:
            ts = datetime.datetime.fromisoformat(str(val.get("ts") or ""))
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            kept[key] = val
    state["sessions"] = kept
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def extract_prompt_terms(prompt: str, max_terms: int = RECALL_TERM_CAP) -> list:
    """把一句自然语言压成检索词：短中文串保留整词，长中文串切 bigram，英文保留标识符。

    这不是「智能分词」，但比把整句当一个 LIKE 模式可靠：中文整句几乎永远匹配不到，
    而 bigram 召回在真实库上已被 search_memories 的放宽重试验证过。返回 [(term, weight)]，
    整词/长标识符权重 2（更具体），bigram 权重 1。
    """
    text = (prompt or "").strip()
    if not text or text.startswith("/"):
        return []
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = _normalize_cn_numbers(text)
    out, seen = [], set()

    def add(term: str, weight: int) -> None:
        term = (term or "").strip().lower()
        if len(term) < 2 or term in seen or term in RECALL_STOP_TERMS:
            return
        if term.isascii() and term in RECALL_ASCII_STOP:
            return
        if term.isdigit() and not (2 <= len(term) <= 4):
            # R825：保留 2–4 位数字（「20 万条」这类量级词有检索价值），
            # 单字符已被上面的 len 拦掉，超长数字串（时间戳/哈希）是噪声
            return
        seen.add(term)
        out.append((term, weight))

    for seg in _WORD_RE.findall(text.lower()):
        # 中英混排（如 dlss文件删掉）会被 \w 连成一段：先拆出 ASCII 标识符，
        # 否则 DLSS 会被切成 dl/ls/ss 三个无意义 bigram。
        for part in re.findall(r"[a-z0-9]+|[^\x00-\x7f]+", seg):
            if part.isascii():
                add(part, 2 if len(part) >= 4 else 1)
                continue
            for chunk in RECALL_CHUNK_SPLIT.split(part):
                if not chunk:
                    continue
                if len(chunk) <= 6:
                    add(chunk, 2)
                    for i in range(0, len(chunk) - 1, 2):
                        add(chunk[i:i + 2], 1)   # 整词不命中时用不重叠 bigram 兜底
                else:
                    for i in range(len(chunk) - 1):
                        add(chunk[i:i + 2], 1)
    out.sort(key=lambda t: -t[1])  # 稳定排序：整词优先，其余按出现顺序
    return out[:max(1, min(int(max_terms), 12))]


def _recall_hit_score(terms: list, text: str) -> int:
    low = (text or "").lower()
    return sum(w for term, w in terms if term in low)


# R825：中文数字规范化。实测「工作台每组只显示二十条没有出口」抽出
# ['出口','工作','作台','台每','组只','只显','显示']——"二十条"整个丢了，而记忆里
# 写的是「20 条」，同一件事两种写法就检索不到。只转带单位的（十/百/千/万），
# 「一样」「一致」里的「一」不碰。
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_CN_NUM_RE = re.compile(r"[零一二两三四五六七八九]?[十百千万][零一二两三四五六七八九十百千万]*")


def _cn_to_int(s: str) -> int:
    total = section = num = 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                total += (section + num) * unit
                section = 0
            else:
                section += (num or 1) * unit
            num = 0
    return total + section + num


_SYS_ENVELOPE_KEYS = ("tool call", "tool_call", "tool_use", "tool_result",
                      "status completed", "task notification", "assistant message",
                      "system-reminder", "task-notification")
_SYS_XML_RE = re.compile(r"^\s*<[a-z][\w:.-]*[\s>/]")


def _looks_like_system_message(text: str) -> bool:
    """判断这段「提示」是不是宿主塞进来的系统消息，而不是用户真说的话。

    R825 实证：journal#8422 记了一次 `terms=task notification 2shlbg tool call
    gm6mv6s4rfriho0iut9y2774 status completed`——一条后台任务完成通知被 UserPromptSubmit
    钩子当成用户提问检索了一遍，白跑一次还带回三条无关记忆。系统消息对「按这句话
    回忆」毫无意义，越早拦越省。

    判据从严：要么是 XML 信封开头，要么**同时命中两个以上**英文工具信封特征词。
    宁可漏拦也不能误杀——用户的英文提问里出现一个 `tool call` 是正常的。
    """
    t = (text or "").strip()
    if not t:
        return False
    if _SYS_XML_RE.match(t):
        return True
    low = t.lower()
    return sum(1 for k in _SYS_ENVELOPE_KEYS if k in low) >= 2


def _normalize_cn_numbers(text: str) -> str:
    if not text:
        return text

    def repl(m):
        s = m.group(0)
        if len(s) < 2:          # 单字「十」「万」多半是语气/量词，不动
            return s
        try:
            return str(_cn_to_int(s))
        except Exception:       # noqa: BLE001 规范化失败原样返回，不影响抽词
            return s

    return _CN_NUM_RE.sub(repl, text)


def _recall_idf(pool_texts: list, terms: list, weighted: bool = True) -> dict:
    """在候选池上估每个检索词的区分度，返回 {term: 加权分}。

    R825 的核心改动。为什么不在停用词表上加词：闭集永远追不完——「围绕/重点/整体」
    补完还会冒出「大致/顺便/顺带」。而**区分度是可以用数据量的**：一个词如果在
    候选池的 30/40 条里都出现（「工作」「问题」「版本」），它命中什么都不能说明
    相关性；只在 1-2 条里出现的词（「teardown」「infobar」）命中就是强信号。

    权重用 BM25 的 idf 形状 log((N+1)/(df+0.5))，再乘抽取时的词权重（整词 2 /
    bigram 1）。池内出现率过半的词压到 0——它们只会把泛化记忆顶上来。

    weighted=False 只返回 log 部分（raw idf），用来判「这条命中里有没有一个真正
    稀有的词」——R826 的分流门槛不能带权重：整词权重 2 会把「版本」这种泛词
    乘到门槛之上（实测就是这样漏掉的）。
    """
    n = max(1, len(pool_texts))
    out = {}
    for term, w in terms:
        if not term:
            continue
        # ASCII 长标识符（pyinstaller / qfluentwidgets / teardown）在中文语料里天然
        # 精确指代，**不参与 df 压零**：本库所有记忆都跟 AgentHub 有关，"qfluentwidgets"
        # 的池内 df 当然高，但它是问题的主体不是噪声（2026-10-31 实测：按 df 压掉它
        # 之后「InfoBar 中文被截断」直接召不回来了）。给一个 2.5 的保底分。
        ascii_ident = term.isascii() and len(term) >= 5
        df = sum(1 for t in pool_texts if term in t)
        raw = math.log((n + 1.0) / (df + 0.5))
        if ascii_ident:
            raw = max(raw, 2.5)
        elif df * 2 > n and n >= 4:    # 过半候选都含它 = 泛词
            raw = 0.0
        out[term] = w * raw if weighted else raw
    return out


def _recall_weighted_hit(terms: list, idf: dict, text: str) -> float:
    low = (text or "").lower()
    return sum(idf.get(term, 0.0) for term, _ in terms if term in low)


_RECALL_OPEN_STATUS = ("pending", "blocked", "in_progress", "claimed", "awaiting_approval")
# R826：一条记忆要被当「依据」注入，它命中的词里至少要有一个**稀有**的（raw idf ≥ 2.0）。
# 40 条候选池里 idf=2.0 对应的 df≈5.5（约 14% 的出现率）；低于这个数的词命中什么都说
# 不明相关性。实测标定：df=2→2.80，df=5→2.01，df=9→1.46，df=20→0.69。
_RECALL_RARE_IDF = 2.0


def _recall_project_block(root: str, pname: str, limit: int = 3) -> list:
    """本项目近况：最近几条工作记录 + 未完成任务数。

    R825 新增，补的是「按词召回」结构性覆盖不到的那一半。实测 2026-10-31：用户在
    项目目录里问「继续迭代」，召回 top3 是三条泛化元记忆，而这个项目「上一轮做到
    哪、还剩什么」一条都没有——它们在 records / tasks 表里，压根不走记忆检索那条
    路。判断依据是 cwd 推断出的项目名（已经在算，此前只用来加 1.0 的分），这里直接
    把它变成一条独立的上下文块：**进了项目目录，先知道这个项目在哪。**
    """
    if not pname:
        return []
    try:
        recs = list_records(root, project=pname, limit=max(1, int(limit)))
    except sqlite3.Error:
        recs = []
    open_n = 0
    try:
        with db_conn(root) as conn:
            marks = ",".join("?" * len(_RECALL_OPEN_STATUS))
            open_n = conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE project=? AND status IN ({marks})",
                (pname, *_RECALL_OPEN_STATUS)).fetchone()[0]
    except sqlite3.Error:
        open_n = 0
    if not recs and not open_n:
        return []
    lines = [f"【本项目近况 · {_brief_oneline(pname, 26)}】"]
    for r in recs:
        day = str(r.get("date") or "")[5:10] or "—"
        who = str(r.get("agent") or "").strip()
        head = _brief_oneline(str(r.get("title") or ""), 46)
        lines.append(f"· {day} {who}：{head}" if who else f"· {day}：{head}")
    if open_n:
        lines.append(f"· 未完成 {open_n} 项（hub_next 可认领）")
    return lines


def prompt_recall(root: str, prompt: str, agent: str = "", cwd: str = "",
                  project: str = "", max_chars: int = RECALL_MAX_CHARS,
                  session: str = "", state_path: str = "",
                  update_state: bool = True) -> dict:
    """按用户这一句话自动回忆（UserPromptSubmit 钩子入口）。

    与 session_brief 分工：简报解决「开局知道团队在干什么」，本函数解决「说到某件事时
    自动想起相关记忆」。只在真命中时返回文本——每句话都塞一段「无相关记忆」纯属噪声。
    记忆与历史记录分开取：记忆给结论，记录给过程；都受字符预算约束。
    """
    t0 = time.time()
    out = {"text": "", "memory_ids": [], "record_ids": [], "terms": [],
           "elapsed_ms": 0, "project": "", "chars": 0, "system_message": False}
    if not root or not os.path.isdir(root):
        return out
    if _looks_like_system_message(prompt):
        # 系统信封（task-notification / tool_result 之类）不是用户提问，按它回忆是空转
        out["system_message"] = True
        return out
    who = _norm_agent(agent) or (agent or "").strip()
    pname = (project or "").strip() or infer_project_from_cwd(root, cwd)
    terms = extract_prompt_terms(prompt)
    if not terms:
        # R825：「继续」「接着弄」「下一步」这类推进语整句都是停用词，抽不出检索词——
        # 而这恰恰是最需要上下文的时候（用户没说清干啥，就是让 AI 自己接上）。此前
        # 这里直接 return，等于在最该开口的一句话上闭嘴。现在只出项目近况。
        proj_only = _recall_project_block(root, pname)
        if proj_only:
            head = ["【AgentHub 即时回忆 · 只说项目近况（这句没抽到检索词）】"]
            text = "\n".join(head + proj_only)
            if len(text) > max_chars:
                text = text[: max(30, max_chars - 24)].rstrip() + "\n…（已截断）"
            out.update(text=text, project=pname, chars=len(text),
                       elapsed_ms=int((time.time() - t0) * 1000))
        return out
    state = {"v": 1, "sessions": {}}
    skey = f"{who or '?'}|{session}" if session else ""
    excluded: set = set()
    state_file = _recall_state_file(state_path)
    if skey:
        state = _load_recall_state(state_file)
        raw_ids = (state["sessions"].get(skey) or {}).get("ids") or []
        excluded = {int(x) for x in raw_ids if str(x).lstrip("-").isdigit()}
    query = " ".join(term for term, _ in terms)
    need = 2 if len(terms) >= 2 else 1
    intent = _recall_intent(prompt)
    kind_weights = RECALL_KIND_WEIGHTS.get(intent, RECALL_KIND_WEIGHTS["general"])
    try:
        # gate=False：覆盖度闸门按「查询词命中比例」删候选，对一句自然语言太严——
        # 真实库实测会把 #487（批处理括号教训，命中 8 分）直接删掉，只留 3 条泛化记忆。
        # 召回层有自己的命中阈值与重排，宽召回 + 严重排比窄召回更可靠。
        mems = search_memories(root, query, limit=40, count_hits=False,
                               semantic=False, gate=False)
    except sqlite3.Error:
        return out
    # R825：先算池内区分度，再打分。这一步不动召回集（谁会进候选），只动排序——
    # 把「命中了一堆泛词」和「命中了一个精确词」区分开。
    mem_texts = [str(r.get("content") or "") + " " + str(r.get("tags") or "")
                 for r in mems]
    mem_idf = _recall_idf(mem_texts, terms)
    mem_idf_raw = _recall_idf(mem_texts, terms, weighted=False)
    scored = []
    for r in mems:
        if r.get("id") in excluded:
            continue
        body = str(r.get("content") or "") + " " + str(r.get("tags") or "")
        hit = _recall_hit_score(terms, body)
        if hit < need:
            continue
        wid = _recall_weighted_hit(terms, mem_idf, body)
        # 这一条命中的词里，最稀有的那个有多稀有（不带权重）——R826 的分流门槛。
        # 必须用小写体比对：正文里写的是「PyInstaller」，检索词全小写，直接 in 会
        # 全部判不中，ASCII 标识符等于没参与打分（2026-10-31 实测踩到）。
        low_body = body.lower()
        best_raw = max((mem_idf_raw.get(t, 0.0) for t, _ in terms if t in low_body),
                       default=0.0)
        r["_recall_hit"] = hit
        r["_recall_w"] = wid
        r["_recall_best"] = best_raw
        kind = r.get("kind") or "note"
        spec = wid * 2.2 + kind_weights.get(kind, 0.4) + (0.8 if r.get("pinned") else 0)
        if pname and (r.get("project") or "") == pname:
            spec += 1.0
        elif not r.get("project"):
            spec += 0.2
        spec += min(int(r.get("use_count") or 0), 10) * 0.04
        try:
            spec += {"fresh": 0.6, "stale": 0.0, "expired": -1.0}.get(
                memory_freshness(r).get("state"), 0.0)
        except Exception:  # noqa: BLE001 新鲜度异常不影响召回
            pass
        scored.append((spec, r))
    scored.sort(key=lambda x: (-x[0], -(x[1].get("use_count") or 0), -x[1].get("id", 0)))
    # R826：**逐条**筛「至少命中一个稀有词」的，而不是整批判弱。
    # 整批判会误伤：提问里既有泛词也有精确词时（「qfluentwidgets 中文被截断」），
    # 排序第一那条可能只命中泛词，整批就被砍了——真正精确的那条明明在第 2 位。
    strong = [(s, r) for s, r in scored
              if float(r.get("_recall_best") or 0.0) >= _RECALL_RARE_IDF]
    picked = [r for _, r in strong[:3]]
    try:
        recs = search_records(root, query, limit=30)
    except sqlite3.Error:
        recs = []
    rec_scored = []
    now_dt = datetime.datetime.now()
    rec_texts = [str(r.get("title") or "") + " " + str(r.get("content") or "") for r in recs]
    rec_idf = _recall_idf(rec_texts, terms)
    rec_idf_raw = _recall_idf(rec_texts, terms, weighted=False)
    for r in recs:
        title = str(r.get("title") or "")
        body = title + " " + str(r.get("content") or "")
        hit = _recall_hit_score(terms, body)
        if hit < need:
            continue
        spec = _recall_weighted_hit(terms, rec_idf, body) * 2.2 \
            + _recall_hit_score(terms, title)
        if pname and (r.get("project") or "") == pname:
            spec += 1.5
        try:
            age = (now_dt - datetime.datetime.strptime(str(r.get("date"))[:10],
                                                       "%Y-%m-%d")).total_seconds() / 86400
            spec += 1.0 if age <= 3 else (0.5 if age <= 14 else 0.0)
        except (ValueError, TypeError):
            pass
        r["_recall_best"] = max(
            (rec_idf_raw.get(t, 0.0) for t, _ in terms if t in body.lower()), default=0.0)
        rec_scored.append((spec, r))
    rec_scored.sort(key=lambda x: (-x[0], -int(x[1].get("id") or 0)))
    rec_rows = [r for _, r in rec_scored
                if float(r.get("_recall_best") or 0.0) >= _RECALL_RARE_IDF][:2]
    proj_lines = _recall_project_block(root, pname)
    top_terms = [t for t, _ in sorted(terms, key=lambda x: -float(mem_idf.get(x[0]) or 0))[:3]]
    search_cmd = f"hub_search(keyword=\"{_brief_oneline(' '.join(top_terms), 60)}\")"
    if not picked and not rec_rows:
        # R826：有候选、但每一条都只命中泛词 —— 这是最该克制的场景。摘要看着像答案、
        # 实际不相关（实测「这个版本显示有问题怎么办」会端出「接入层通用化」「审批队列」
        # 三条），模型照着答就是自信地跑偏。此时只给检索词，让模型自己去查。
        # 依据：逐句注入有用率 18.2% vs 主动拉取 72.4%（Nlm Memory 因此默认关闭逐句注入）；
        # mem9「nudge once per session, not every turn」——每轮塞同款文字会让模型免疫。
        if mems:
            lines = ["【AgentHub 即时回忆 · 库里没有能当结论的东西】",
                     f"这句只碰上泛词，别把下面当依据——要动这块之前先自己查：{search_cmd}"]
            if proj_lines:
                lines.extend(proj_lines)
            text = "\n".join(lines)
            if len(text) > max_chars:
                text = text[: max(30, max_chars - 24)].rstrip() + "\n…（已截断）"
            out.update(text=text, project=pname, chars=len(text),
                       elapsed_ms=int((time.time() - t0) * 1000))
            return out
        # R825：连候选都没有 ≠ 没有上下文。「继续迭代」抽到「迭代」却在库里查不到，
        # 结论不该是闭嘴——在项目目录里，本项目最近做到哪比任何记忆都该先说。
        if proj_lines:
            text = "\n".join(
                ["【AgentHub 即时回忆 · 没检索到相关记忆，只给项目近况】"] + proj_lines)
            if len(text) > max_chars:
                text = text[: max(30, max_chars - 24)].rstrip() + "\n…（已截断）"
            out.update(text=text, project=pname, chars=len(text),
                       elapsed_ms=int((time.time() - t0) * 1000))
        else:
            out["terms"] = [t for t, _ in terms]
            out["elapsed_ms"] = int((time.time() - t0) * 1000)
        return out
    lines = ["【AgentHub 即时回忆 · 自动检索，无需提醒】",
             "这是索引不是全文：要动具体命令/路径/参数前先 hub_search 取原文，"
             "别照着摘要复述；与现状冲突时以现状为准，收工时顺手更正。"]
    if proj_lines:
        lines.extend(proj_lines)
    ids = []
    for r in picked:
        ids.append(r.get("id"))
        used = int(r.get("use_count") or 0)
        lines.append(f"· #{r.get('id')} [{r.get('kind') or 'note'}] "
                     f"{_brief_oneline(str(r.get('content') or ''), 150)}"
                     f"（命中 {r.get('_recall_hit') or 0}；"
                     f"用过 {used} 次）")
    if rec_rows:
        lines.append("相关历史：")
        for r in rec_rows:
            head = _brief_title(str(r.get("title") or "")) or _brief_oneline(
                str(r.get("content") or ""), 40)
            lines.append(f"- [{_brief_oneline(str(r.get('project') or ''), 22)}] "
                         f"{_brief_oneline(head, 42)}"
                         f"（{r.get('agent') or '其他'} · {r.get('date') or '—'}）")
    lines.append(f"更多：hub_memory_read(query=\"{_brief_oneline(' '.join(top_terms) or query, 60)}\")")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max(30, max_chars - 24)].rstrip() + "\n…（已截断）"
    out.update(text=text, memory_ids=[i for i in ids if i is not None],
               record_ids=[r.get("id") for r in rec_rows], terms=[t for t, _ in terms],
               project=pname, chars=len(text))
    if out["memory_ids"]:
        try:
            _bump_access(root, out["memory_ids"])
        except sqlite3.Error:
            pass
    try:
        log_search(root, "prompt_recall", query[:200], len(out["memory_ids"]), agent=who)
    except sqlite3.Error:
        pass
    if skey and update_state and out["memory_ids"]:
        entry = (state["sessions"].get(skey) or {"ids": []})
        entry["ids"] = list(dict.fromkeys(list(entry.get("ids") or [])
                                          + [str(i) for i in out["memory_ids"]]))[-200:]
        entry["ts"] = _now()
        state["sessions"][skey] = entry
        _save_recall_state(state_file, state)
    out["elapsed_ms"] = int((time.time() - t0) * 1000)
    return out


def session_brief(root: str, agent: str = "", cwd: str = "", project: str = "",
                  max_chars: int = BRIEF_MAX_CHARS, source: str = "session") -> dict:
    """会话启动简报：无需用户提醒，AI 一开局就看到的上下文快照。

    给 SessionStart 钩子与 hub_brief 共用。只读、失败不抛（钩子必须 fail-open），
    整体有字符预算——注入的长上下文会挤掉真正的工作内容，简报只负责「知道去哪查」。

    source="initialize" 用于 MCP 握手注入：握手没有 cwd，标题与来源说明随之改写，
    避免在 agent 里声称「钩子注入」而实际来自 initialize（口径不能骗人）。

    返回 {"text", "project", "truncated", "elapsed_ms", "stats"}；空根目录返回空 text。
    """
    t0 = time.time()
    out = {"text": "", "project": "", "truncated": False, "elapsed_ms": 0,
           "stats": {}}
    if not root or not os.path.isdir(root):
        return out
    try:
        who = _norm_agent(agent) or (agent or "").strip()
        pname = (project or "").strip() or infer_project_from_cwd(root, cwd)
        now = datetime.datetime.now()
        with db_conn(root) as conn:
            n_proj = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            n_rec = conn.execute("SELECT COUNT(*) FROM records WHERE status='active'").fetchone()[0]
            n_mem = conn.execute("SELECT COUNT(*) FROM memories WHERE " + _live_sql()).fetchone()[0]
            pinned = [dict(r) for r in conn.execute(
                "SELECT id,kind,content FROM memories WHERE " + _live_sql()
                + " AND pinned=1 ORDER BY updated DESC, id DESC LIMIT 3")]
            proj = None
            proj_recs, proj_mem = [], []
            if pname:
                proj = conn.execute(
                    "SELECT name, (SELECT MAX(date) FROM records WHERE project=? AND status='active') AS last_active,"
                    " (SELECT COUNT(*) FROM records WHERE project=? AND status='active') AS n_records"
                    " FROM projects WHERE name=?", (pname, pname, pname)).fetchone()
                if not proj:
                    pname = ""
                else:
                    proj = dict(proj)
                    proj_recs = [dict(r) for r in conn.execute(
                        "SELECT date,agent,title,content FROM records WHERE project=? AND status='active'"
                        " ORDER BY date DESC, id DESC LIMIT 2", (pname,))]
                    like = f"%{_like_escape(pname)}%"
                    proj_mem = [dict(r) for r in conn.execute(
                        "SELECT id,kind,content FROM memories WHERE " + _live_sql()
                        + " AND (project=? OR tags LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')"
                        " ORDER BY pinned DESC, updated DESC, id DESC LIMIT 4",
                        (pname, like, like))]
        progress = recent_diverse(root, 5, pool=120)
        seen: set = set()
        sig = signal_unread_breakdown(root, who) if who else {"fresh": 0, "stale": 0, "total": 0}
        sig_rows = []
        if who and sig["fresh"]:
            sig_rows = [r for r in signal_inbox(root, who, unread_only=True, limit=6,
                                                exclude_stale=True)
                        if (r.get("from_agent") or "") != who][:2]
        tasks = task_next(root, owner=who, limit=2) if who else {"counts": {}}
        tcounts = tasks.get("counts") or {}

        via = "MCP 握手" if source == "initialize" else "SessionStart 钩子"
        lines = [f"【AgentHub 大脑简报 · 会话启动自动生成 · {who or '未署名'} · "
                 f"{now:%Y-%m-%d %H:%M}】",
                 f"根目录 {root} ｜ {n_proj} 项目 · {n_rec} 条活跃记录 · {n_mem} 条记忆",
                 f"（这份简报由{via}自动送达，无需用户提醒；细节按下面入口再查，别凭空开工）"]
        if progress:
            lines.append("")
            lines.append("▸ 团队最近进展")
            for r in progress:
                head = _brief_title(r["title"]) or record_outline(r.get("content", ""))
                lines.append(f"- [{_brief_oneline(r['project'], 26)}] "
                             f"{_brief_oneline(head, 42)}"
                             f"（{r['agent'] or '其他'} · {r['date']}）")
        if pname and proj:
            lines.append("")
            lines.append(f"▸ 当前项目：{pname}")
            lines.append(f"  {proj['n_records']} 条活跃记录，最近 {proj['last_active'] or '—'}；"
                         f"开工前先看最近记录再动手。")
            for r in proj_recs:
                head = _brief_title(r["title"]) or record_outline(r.get("content", ""))
                lines.append(f"  - {_brief_oneline(head, 40)}"
                             f"（{r['agent'] or '其他'} · {r['date']}）")
            seen = set()
            for m in proj_mem:
                if m["id"] in seen:
                    continue
                seen.add(m["id"])
                lines.append(f"  · #{m['id']} [{m['kind']}] {_brief_oneline(m['content'], 90)}")
            if proj_recs or proj_mem:
                lines.append(f"  更多：hub_memory_read(query=\"{pname} 关键词\") / hub_search(keyword=…)")
        if pinned:
            lines.append("")
            lines.append("▸ 置顶记忆（主人/团队重点）")
            for m in pinned:
                if m["id"] in seen:
                    continue
                lines.append(f"- #{m['id']} [{m['kind']}] {_brief_oneline(m['content'], 110)}")
        todo = []
        if sig["fresh"]:
            todo.append(f"- 未读信号 {sig['fresh']} 条"
                        + (f"（另有陈旧 {sig['stale']} 条）" if sig["stale"] else ""))
            for r in sig_rows:
                todo.append(f"  · 来自 {r.get('from_agent') or '广播'}：{_brief_oneline(r.get('content'), 70)}")
            todo.append("  处理：hub_signals() 看全量，读完 hub_signal_read")
        for t in (tasks.get("doing") or [])[:2]:
            todo.append(f"- 我在办 #{t['id']} {_brief_oneline(t.get('title'), 40)}"
                        f"（{t.get('status')}）")
        if tcounts.get("ready"):
            todo.append(f"- 可认领 {tcounts['ready']} 条：hub_next(agent=\"{who}\")")
        if todo:
            lines.append("")
            lines.append("▸ 待你处理")
            lines.extend(todo)
        lines.append("")
        lines.append("▸ 收工约定")
        lines.append("- hub_log_work(project, agent, content)：目的/做了什么/验证结果/如何回滚；"
                     "有跨会话价值的知识再 hub_memory_write；收尾 hub_ops_run 自检。"
                     "钩子会拦截「干了活却没记录」。")
        text = "\n".join(lines)
        truncated = len(text) > max_chars
        if truncated:
            text = text[: max_chars - 24].rstrip() + "\n…（简报超预算，已截断）"
        out.update({"text": text, "project": pname, "truncated": truncated,
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "stats": {"projects": n_proj, "records": n_rec, "memories": n_mem,
                              "unread": sig["fresh"], "ready": tcounts.get("ready", 0)}})
        return out
    except (sqlite3.Error, OSError, ValueError):
        return out


def search_tasks(root: str, kw: str, limit: int = 20) -> list:
    """任务检索（多词 AND）：标题/详情/项目/负责人/最近错误任一字段命中。
    排序：在办 → 阻塞 → 待办 → 完成 → 终态，同状态按优先级与 id。"""
    words = [w for w in re.split(r"\s+", (kw or "").strip()) if w][:8]
    if not words:
        return []
    fields = ("title", "detail", "project", "owner", "last_error")
    conds = []
    args: list = []
    for w in words:
        like = f"%{_like_escape(w)}%"
        conds.append("(" + " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in fields) + ")")
        args += [like] * len(fields)
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id,project,title,detail,status,owner,due,priority,created,updated,"
            "done_at,attempts,last_error FROM tasks WHERE " + " AND ".join(conds)
            + " ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'blocked' THEN 1"
              " WHEN 'pending' THEN 2 WHEN 'done' THEN 3 ELSE 4 END, priority, id DESC LIMIT ?",
            args + [max(1, min(limit, 100))])]


def search_errors(root: str, kw: str, limit: int = 20) -> list:
    """错误/踩坑检索（多词 AND）：标题/详情/agent/项目/回滚方式任一字段命中。
    排序：未关闭在前（同类按 id 新→旧）——先看还开着的坑，再看已修的方案。"""
    words = [w for w in re.split(r"\s+", (kw or "").strip()) if w][:8]
    if not words:
        return []
    fields = ("title", "detail", "agent", "project", "undo")
    conds = []
    args: list = []
    for w in words:
        like = f"%{_like_escape(w)}%"
        conds.append("(" + " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in fields) + ")")
        args += [like] * len(fields)
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id,ts,agent,project,title,detail,undo,status FROM errors WHERE "
            + " AND ".join(conds)
            + " ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, id DESC LIMIT ?",
            args + [max(1, min(limit, 100))])]


def search_all(root: str, kw: str, limit: int = 30) -> dict:
    """全脑检索：工作记录 + 记忆 + 随记 + 任务 + 错误 + 文件名。
    返回 {"records": [...], "memories": [...], "notes": [...], "tasks": [...], "errors": [...], "files": [...]}"""
    if not kw.strip():
        return {"records": [], "memories": [], "notes": [], "tasks": [], "errors": [], "files": []}
    with _df_memo_scope():
        out = {"records": search_records(root, kw, limit=limit),
               "memories": search_memories(root, kw, limit=limit),  # 必须关键字：search_memories 第三参是 kind，
               "notes": search_notes(root, kw, limit),             # 位置传 limit 会把 limit 塞进 kind（2026-10-13 实测）
               "tasks": search_tasks(root, kw, limit),
               "errors": search_errors(root, kw, limit),
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


def skills_by_agent(root: str, agent: str = "") -> list:
    """从技能索引按 agent 取行（hub_list_skills 的兜底数据源）。

    本地目录探测不到（agent 装在别处/目录改名）时，索引里仍有上次同步的记录。
    索引只增不删，路径可能已失效——调用方必须标注「来自索引」而不是当作现场探测。
    """
    sql = "SELECT name, agent, path, desc, hits, last_hit FROM skills"
    args: list = []
    if agent:
        sql += " WHERE lower(agent) LIKE ?"
        args.append(f"%{agent.lower()}%")
    sql += " ORDER BY agent, name"
    try:
        with db_conn(root) as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []


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
NOTE_MOODS = ("", "兴奋", "笃定", "疑问", "纠结", "灵感", "低落", "平静")


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
                parts.append(f"项目「{proj}」最后活跃 {gp.get('last_active','?')}，"
                             f"共 {gp.get('total', 0)} 条记录，最近：")
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
    mood = mood if mood in NOTE_MOODS else ""
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
# R47 审计链：journal 不只是日志——每次写操作都要能在事后证明没被改过。
# 每行带 prev_hash + hash（sha256(prev + 字段)），追加与主操作同事务；单行改字段会让
# 自身 hash 对不上，删行/调序会让后继 prev_hash 断层，尾部截断由 meta.journal_head 抓。
# 威胁模型是「悄悄改一条流水」，不是防有整库写权限的人（他可整库重算）——不夸大。

def _journal_hash(prev_hash: str, ts: str, agent: str, action: str,
                  target: str, backup: str, note: str) -> str:
    raw = "\x1f".join(str(x or "") for x in
                      (prev_hash, ts, agent, action, target, backup, note))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _journal_last_hash(conn) -> str:
    row = conn.execute("SELECT hash FROM journal WHERE hash IS NOT NULL AND hash!='' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return (row["hash"] if row else "") or ""


def _journal_insert_plain(conn, ts, agent, action, target, backup, note) -> None:
    conn.execute("INSERT INTO journal(ts,agent,action,target,backup,note) VALUES(?,?,?,?,?,?)",
                 (ts, agent, action, target, backup, note))


def _journal_append(conn, ts: str, agent: str, action: str, target: str = "",
                    backup: str = "", note: str = "") -> str:
    """同事务追加一条接链流水。抢不到写锁或旧库缺列时降级为普通写入（旁路 fail-open）。"""
    try:
        if not conn.in_transaction and _begin_write(conn):
            _journal_insert_plain(conn, ts, agent, action, target, backup, note)
            return ""
        prev = _journal_last_hash(conn)
        h = _journal_hash(prev, ts, agent, action, target, backup, note)
    except sqlite3.Error:
        _journal_insert_plain(conn, ts, agent, action, target, backup, note)
        return ""
    conn.execute("INSERT INTO journal(ts,agent,action,target,backup,note,prev_hash,hash) "
                 "VALUES(?,?,?,?,?,?,?,?)", (ts, agent, action, target, backup, note, prev, h))
    try:
        _meta_set_conn(conn, "journal_head", h)
        conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('journal_chain_at',?)", (ts,))
    except sqlite3.Error:
        pass
    return h


def _journal_seal_backlog(conn) -> int:
    """把最后一次封存之后的未封存流水按 id 顺序接链（首次升级=封存全部历史）。
    只处理尾部未封存段：夹在已封存行中间的未封存行不接链——接它会改写后继的 prev，
    等于替篡改者抹平断层。这类行在验证里计入「历史未封存」。"""
    last = conn.execute("SELECT id, hash FROM journal WHERE hash IS NOT NULL AND hash!='' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    start_id = last["id"] if last else 0
    prev = (last["hash"] if last else "") or ""
    rows = list(conn.execute(
        "SELECT id, ts, agent, action, target, backup, note FROM journal "
        "WHERE id>? AND (hash IS NULL OR hash='') ORDER BY id", (start_id,)))
    for r in rows:
        h = _journal_hash(prev, r["ts"], r["agent"], r["action"],
                          r["target"], r["backup"], r["note"])
        conn.execute("UPDATE journal SET prev_hash=?, hash=? WHERE id=?", (prev, h, r["id"]))
        prev = h
    if rows:
        _meta_set_conn(conn, "journal_head", prev)
        _meta_set_conn(conn, "journal_chain_at", _now())
    return len(rows)


def _journal_row(conn, agent: str, action: str, target: str = "", note: str = "") -> None:
    """与主操作同事务写流水（审计旁路：流水失败不拖垮主操作，也不另开连接避免写锁）。"""
    try:
        _journal_append(conn, _now(), _norm_agent(agent) or "", action, target, "",
                        (note or "")[:500])
    except sqlite3.Error:
        pass


def journal_add(root: str, agent: str, action: str, target: str = "",
                backup: str = "", note: str = "") -> None:
    try:
        with db_conn(root) as conn:
            _journal_append(conn, _now(), _norm_agent(agent) or "", action, target,
                            backup, note)
    except Exception:  # noqa: BLE001 流水是旁路
        pass


def journal_verify(root: str, max_breaks: int = 5) -> dict:
    """校验审计链（只读）。返回 ok/total/sealed/legacy/broken/head/anchor/chain_at/summary。
    ok=False 的三种形态：内容被改、链断层、头锚不一致；全库无流水视为健康空态。"""
    with db_conn(root) as conn:
        meta = {r["key"]: r["value"] for r in conn.execute(
            "SELECT key,value FROM meta WHERE key IN ('journal_head','journal_chain_at')")}
        total = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        prev, last_id, sealed, legacy, breaks = "", 0, 0, 0, 0
        broken: list = []
        for r in conn.execute(
                "SELECT id,ts,agent,action,target,backup,note,prev_hash,hash "
                "FROM journal ORDER BY id"):
            stored = (r["hash"] or "").strip()
            if not stored:
                legacy += 1
                continue
            sealed += 1
            last_id = r["id"]
            if (r["prev_hash"] or "").strip() != prev:
                breaks += 1
                if len(broken) < max_breaks:
                    broken.append({"id": r["id"], "why": "prev_hash 断层（删行/调序/改链）"})
            else:
                expect = _journal_hash(prev, r["ts"], r["agent"], r["action"],
                                       r["target"], r["backup"], r["note"])
                if expect != stored:
                    breaks += 1
                    if len(broken) < max_breaks:
                        broken.append({"id": r["id"], "why": "内容与 hash 不符（字段被改）"})
            prev = stored
        anchor = (meta.get("journal_head") or "").strip()
        if sealed and anchor != prev:
            breaks += 1
            if len(broken) < max_breaks:
                broken.append({"id": last_id, "why": "头锚不一致（尾部截断/锚被改）"})
        ok = breaks == 0 and (sealed > 0 or total == 0)
        if total == 0:
            summary = "审计链空（尚无流水）"
        elif breaks:
            ids = "、#".join(str(b["id"]) for b in broken if b.get("id"))
            summary = f"审计链异常：{breaks} 处" + (f"（#{ids}）" if ids else "")
        elif sealed == 0:
            summary = f"审计链未封存：{legacy} 条历史流水仍无哈希（跑 init_db 升级）"
        else:
            summary = f"审计链完整：{sealed} 条已封存" + (
                f"，{legacy} 条历史未封存" if legacy else "")
        return {"ok": ok, "total": total, "sealed": sealed, "legacy": legacy,
                "breaks": breaks, "broken": broken, "head": prev,
                "anchor": anchor, "chain_at": meta.get("journal_chain_at", ""),
                "summary": summary}


def journal_list(root: str, limit: int = 200) -> list:
    with db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM journal ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))]


SECURITY_WINDOW_DAYS = 7  # R90 安全态势默认窗口：拦截是低频事件，7 天足够发现配置漂移


def security_snapshot(root: str, days: int = SECURITY_WINDOW_DAYS) -> dict:
    """R90 越权拦截可观测（只读）：聚合近 N 天 journal 的「越权拦截」。

    拒绝本身已在发生处留痕，但散在流水里没人看——管理者无法回答「谁在冒名、
    哪个工具被反复尝试」。按工具归并，区分「未锁身份」与「角色越权」；
    时间窗口按 ISO 字符串比较，坏时间不猜（ts 为空自然落在窗口外）。
    """
    try:
        days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days = SECURITY_WINDOW_DAYS
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat(timespec="seconds")
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,ts,agent,action,target,note FROM journal "
            "WHERE action='越权拦截' AND ts>=? ORDER BY id DESC", (cutoff,))]
    by_tool: dict = {}
    by_agent: dict = {}
    for r in rows:
        tool = (r.get("target") or "").split(":", 1)[0] or "-"
        slot = by_tool.setdefault(tool, {"tool": tool, "count": 0, "identity": 0,
                                         "role": 0, "last": ""})
        slot["count"] += 1
        note = r.get("note") or ""
        kind = ("identity" if "未锁定" in note
                or (r.get("target") or "").endswith(":identity") else "role")
        slot[kind] += 1
        if (r.get("ts") or "") > slot["last"]:
            slot["last"] = r.get("ts") or ""
        ag = (r.get("agent") or "").strip().lower() or "unknown"
        aslot = by_agent.setdefault(ag, {"agent": ag, "count": 0, "identity": 0,
                                         "role": 0, "last": ""})
        aslot["count"] += 1
        aslot[kind] += 1
        if (r.get("ts") or "") > aslot["last"]:
            aslot["last"] = r.get("ts") or ""
    recent = [{"ts": r.get("ts") or "", "agent": r.get("agent") or "unknown",
               "target": r.get("target") or "-", "note": (r.get("note") or "")[:120]}
              for r in rows[:5]]
    return {"days": days, "window_start": cutoff, "total": len(rows),
            "identity": sum(x["identity"] for x in by_tool.values()),
            "role": sum(x["role"] for x in by_tool.values()),
            "by_tool": sorted(by_tool.values(), key=lambda x: (-x["count"], x["tool"])),
            "by_agent": sorted(by_agent.values(), key=lambda x: (-x["count"], x["agent"])),
            "recent": recent}


def error_add(root: str, agent: str, title: str, detail: str = "",
              project: str = "", undo: str = "", return_info: bool = False):
    """登记一条错误；同一 (agent, 项目, 标题) 且仍 open 的错误**合并计数**而不是新增行。

    R261：本机 19 条错误里 6 条是同一句「MCP 工具 hub_ops_run 执行异常」——既看不出
    「它到底发生了多少次」（这恰恰是最重要的信号：偶发 1 次和连环 6 次是两种病），
    又把列表刷成噪音。改法：同签名 open 错误 → repeat_count+1 + last_ts 刷新，
    detail/undo 保留**首次**那份（首次现场最完整）；status 已 fixed 后再发生仍新增行
    （复发是独立事件，不该被历史计数吃掉）。
    R262：合并时**另存 last_detail**（最近一次现场）——只留首次会丢掉「错误变形成什么样」，
    首次和最近各存一份才既有起点又有走向；`return_info=True` 返回 {id, merged, repeat_count}
    让调用方能把「这是第几次」回给登记人（否则合并对使用者完全不可见）。
    并发：查-改串行在同一把写锁里（BEGIN IMMEDIATE），否则两个进程各插一条（R15 同型竞态）。
    """
    if not title or not title.strip():
        return "title 必填"
    title = title.strip()[:200]
    with db_conn(root) as conn:
        agent = _norm_agent(agent)
        _upsert_agent(conn, agent, project)
        # _upsert_agent 通常已经开了隐式事务（它要写 agents 行）；只有它什么都没写时才补
        # 一个显式写锁——否则 "cannot start a transaction within a transaction"。
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        dup = conn.execute(
            "SELECT id FROM errors WHERE status='open' AND agent=? AND title=? "
            "AND COALESCE(project,'')=?", (agent or "unknown", title, project or "")).fetchone()
        if dup:
            detail = (detail or "")[:4000]
            conn.execute(
                "UPDATE errors SET repeat_count=COALESCE(repeat_count,1)+1, last_ts=?, "
                "last_detail=CASE WHEN ?<>'' THEN ? ELSE COALESCE(last_detail,'') END WHERE id=?",
                (_now(), detail, detail, dup["id"]))
            n = conn.execute("SELECT repeat_count FROM errors WHERE id=?",
                             (dup["id"],)).fetchone()["repeat_count"]
            if return_info:
                return {"id": dup["id"], "merged": True, "repeat_count": int(n or 2)}
            return ""
        cur = conn.execute("INSERT INTO errors(ts,agent,project,title,detail,undo,status,"
                           "repeat_count,last_ts,last_detail) VALUES(?,?,?,?,?,?,'open',1,?,?)",
                           (_now(), agent or "unknown", project or "", title,
                            (detail or "")[:4000], undo or "", _now(), (detail or "")[:4000]))
        new_id = cur.lastrowid
    if return_info:
        return {"id": new_id, "merged": False, "repeat_count": 1}
    return ""


def error_count(root: str, status: str = "") -> int:
    """错误总数（R267）：error_list 有 LIMIT，列表抬头不能把取到的条数当总数。"""
    q, args = "SELECT COUNT(*) AS n FROM errors", []
    if status in ("open", "fixed"):
        q += " WHERE status=?"
        args.append(status)
    with db_conn(root) as conn:
        return int(conn.execute(q, args).fetchone()["n"] or 0)


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

HANDOFF_STALE_DAYS = 3  # R88 未认领交接超过该天数标陈旧（只提示复核，不自动关闭）


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
        rows = [dict(r) for r in conn.execute(q, args)]
    return handoff_annotate(rows)


def handoff_annotate(rows: list, now=None) -> list:
    """R88 交接时效：补 _age_h / _age_days / _stale。时间不可解析不猜；
    只有 open（待认领）且 ≥ HANDOFF_STALE_DAYS 天算陈旧，已认领不误标。"""
    now = now or datetime.datetime.now()
    for r in rows:
        age_h = _age_hours((r.get("created") or "").strip(), now)
        r["_age_h"] = age_h
        r["_age_days"] = round(age_h / 24, 1) if age_h is not None else None
        r["_stale"] = bool(r.get("status") == "open" and age_h is not None
                           and age_h >= HANDOFF_STALE_DAYS * 24)
    return rows


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
            row = conn.execute(
                "SELECT status, claimed_by, closed_by, close_note FROM handoffs WHERE id=?",
                (handoff_id,)).fetchone()
            if not row:
                return f"交接 #{handoff_id} 不存在"
            if row["status"] == "closed":
                return (f"交接 #{handoff_id} 已作废（{row['closed_by'] or '他人'}："
                        f"{row['close_note'] or '无原因'}），不可认领")
            return f"交接 #{handoff_id} 已被 {row['claimed_by'] or '他人'} 认领（status={row['status']}），不可重复认领"
    journal_add(root, agent, "认领交接", target=f"handoffs#{handoff_id}")
    return f"已认领交接 #{handoff_id}（接手人：{agent}）"


def close_handoff(root: str, handoff_id, agent: str, note: str) -> str:
    """作废未认领交接（open → closed）：必须留原因与操作者，只关 open。
    已认领的交接是「已发生的事实」，不能作废——那会抹掉接手人与认领时间；
    接手后要终止，应新建一条交接或发信号说明。返回错误或「已关闭交接 #id」。"""
    if not isinstance(handoff_id, int) or isinstance(handoff_id, bool) or handoff_id <= 0:
        return f"交接 id 非法：{handoff_id!r}"
    agent = _norm_agent(agent)
    if not agent:
        return "agent 必填"
    note = (note or "").strip()
    if not note:
        return "note 必填（说明作废原因，便于追溯）"
    with db_conn(root) as conn:
        cur = conn.execute(
            "UPDATE handoffs SET status='closed', closed_by=?, closed_at=?, close_note=? "
            "WHERE id=? AND status='open'", (agent, _now(), note[:1000], handoff_id))
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT status, claimed_by, closed_by, closed_at, close_note "
                "FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
            if not row:
                return f"交接 #{handoff_id} 不存在"
            if row["status"] == "claimed":
                return (f"交接 #{handoff_id} 已被 {row['claimed_by'] or '他人'} 认领，"
                        "不能作废（接手已发生；如确需终止，请新建交接或发信号说明）")
            if row["status"] == "closed":
                return (f"已关闭交接 #{handoff_id}（此前由 {row['closed_by'] or '他人'} 于 "
                        f"{row['closed_at'] or '时间未知'} 作废："
                        f"{row['close_note'] or '无原因'}）")
            return f"交接 #{handoff_id} 当前状态 {row['status']}，不可作废"
    journal_add(root, agent, "关闭交接", target=f"handoffs#{handoff_id}",
                note=note[:120])
    return f"已关闭交接 #{handoff_id}（操作者 {agent}，作废原因：{note[:120]}）"


# ---------------------------------------------------------------- 信号（M3）

SIGNAL_KINDS = ("msg", "ack_request", "alert")
# R77：未读信号的陈旧阈值（天）。信号是「开工推送」的燃料——一条没人读的信号会
# 在每次心跳里原样再推一遍，永远占着未读名额（真实库实测：#2 三天前、#1 七天前
# 仍 unread，signal_reads 全表 0 行），越积越多最后「N 条未读」本身就变成噪音，
# 新信号反而被挤出 limit=2 的推送窗口。超阈值的不再计入开工推送，但**不静默丢弃**：
# 收件箱仍显示并标「陈旧」，心跳给一句汇总。不自动标已读——那会伪造送达回执。
SIGNAL_STALE_DAYS = 7


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


def _signal_age_days(ts) -> float:
    """信号年龄（天）。时间不可解析时返回 -1——**不猜**：调用方按「年龄未知=不算陈旧」
    处理，宁可多推一条也不静默吞掉一条没人读的信号。"""
    dt = _parse_ts(ts)
    if dt is None:
        return -1.0
    return (datetime.datetime.now() - dt).total_seconds() / 86400.0


def signal_inbox(root: str, agent: str, unread_only: bool = False,
                 limit: int = 30, exclude_stale: bool = False) -> list:
    """收件箱：发给我的 + 广播（to 空），新的在前；每行附 _read（我是否已读）与
    _stale（是否超过 SIGNAL_STALE_DAYS 天未读）。unread_only=True 只要未读；
    exclude_stale=True 再滤掉陈旧未读（开工推送用，避免旧信号永远占着推送名额）。
    自己发的广播也在（群里自己发的也显示）。

    R272：未读过滤下推到 SQL（NOT EXISTS）——先取 30 条再在 Python 里筛未读，
    30 条全读过时会返回空，而未读的老信号明明还在（「未读列表是空的」比不完整更可怕）。"""
    who = _norm_agent(agent)
    if not who:
        return []
    lim = max(1, min(int(limit or 30), 200))
    with db_conn(root) as conn:
        if unread_only:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM signals WHERE (to_agent=? OR to_agent='') AND NOT EXISTS "
                "(SELECT 1 FROM signal_reads rr WHERE rr.signal_id=signals.id AND rr.agent=?) "
                "ORDER BY id DESC LIMIT ?", (who, who, lim))]
        else:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM signals WHERE to_agent=? OR to_agent='' "
                "ORDER BY id DESC LIMIT ?", (who, lim))]
        for r in rows:
            r["_read"] = bool(conn.execute(
                "SELECT 1 FROM signal_reads WHERE signal_id=? AND agent=?",
                (r["id"], who)).fetchone())
            r["_stale"] = (not r["_read"]) and _signal_age_days(r.get("ts")) > SIGNAL_STALE_DAYS
        if exclude_stale:
            rows = [r for r in rows if not r.get("_stale")]
    return rows


def signal_count(root: str, agent: str, unread_only: bool = False) -> int:
    """收件箱总数（R272）：列表带 LIMIT，抬头写「信号 N 条」不能是取到的条数。
    自己发的广播计入（与 signal_inbox 同口径），未读口径含陈旧（总账用）。"""
    who = _norm_agent(agent)
    if not who:
        return 0
    q = "SELECT COUNT(*) FROM signals WHERE (to_agent=? OR to_agent='')"
    args = [who]
    if unread_only:
        q += (" AND NOT EXISTS (SELECT 1 FROM signal_reads rr "
              "WHERE rr.signal_id=signals.id AND rr.agent=?)")
        args.append(who)
    with db_conn(root) as conn:
        return int(conn.execute(q, args).fetchone()[0])


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


def signal_unread_count(root: str, agent: str, include_stale: bool = False) -> int:
    """未读数（心跳开工推送用）：发给我的 + 广播里我没读的。
    排除自己发的广播——提醒自己去读自己发的消息没有意义（收件箱仍显示，留痕用）。

    include_stale 默认 False：超过 SIGNAL_STALE_DAYS 天仍未读的不计入（R77）——
    它们长期没人理，继续按「新消息」推只会把开工推送变成噪音，还会挤掉 limit=2
    里真正新的信号。要总账用 include_stale=True，或在 hub_signals 里看带「陈旧」
    标记的行。**不自动标已读**：那等于伪造送达回执，宁可留在收件箱里可见。"""
    return signal_unread_breakdown(root, agent)["total" if include_stale else "fresh"]


def signal_unread_breakdown(root: str, agent: str) -> dict:
    """未读数拆成 fresh / stale / total（R77）：开工推送只推 fresh，stale 给一句
    汇总而不是整条内容，total 用于「我到底积了多少」的总账。"""
    who = _norm_agent(agent)
    empty = {"fresh": 0, "stale": 0, "total": 0}
    if not who:
        return dict(empty)
    rows = signal_inbox(root, who, unread_only=True, limit=200)
    # 沿用旧口径：自己发的广播不计（提醒自己去读自己发的没意义）
    rows = [r for r in rows if r.get("from_agent") != who]
    fresh = sum(1 for r in rows if not r.get("_stale"))
    stale = sum(1 for r in rows if r.get("_stale"))
    return {"fresh": fresh, "stale": stale, "total": fresh + stale}


# ---------------------------------------------------------------- 检查点（M4）

CHECKPOINT_KINDS = ("manual", "auto_claim")


def checkpoint_save(root: str, task_id, agent: str, content: str,
                    kind: str = "manual") -> tuple:
    """M4 存检查点：任务级进展快照（断点续跑用）。返回 (错误或 "", 检查点 id)。

    kind=auto_claim 由 task_claim 自动存（开工资锚点）；manual 由 agent 在长任务
    执行中主动存。任务必须存在（防把检查点挂到幽灵任务上）。

    R8 围栏（2026-10-21）：在办任务只收持有者的检查点——接管后旧进程的迟到补写
    会把别人的进展时间线搅浑；写入成功同时续租（写检查点=还在干活，不该因 2h
    没更新任务就被判死）。条件 UPDATE 与 INSERT 同事务，接管竞争只可能一方成功。
    R2：在办但无主的任务不收检查点（不能借写进展「隐身持有」）——先 hub_claim 认领。"""
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
        row = conn.execute("SELECT status,owner FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            return f"任务 #{tid} 不存在", 0
        if row["status"] == "in_progress":
            owner_s = (row["owner"] or "").strip()
            if not owner_s:
                return (f"检查点未写入：任务 #{tid} 在办但无持有者；"
                        "先 hub_claim 认领再写进展"), 0
            if owner_s != who:
                return (f"检查点未写入：任务 #{tid} 由 {row['owner']} 持有"
                        "（接管后旧持有者的补写会污染进展时间线）"), 0
            now = _now()
            cur = conn.execute(
                "UPDATE tasks SET claimed_at=?, updated=?, progress_at=? "
                "WHERE id=? AND status='in_progress' AND owner=?",
                (now, now, now, tid, who))
            if cur.rowcount != 1:
                fresh = conn.execute(
                    "SELECT status,owner FROM tasks WHERE id=?", (tid,)).fetchone()
                return (f"检查点未写入：任务状态/持有者已变化"
                        f"（当前 {fresh['status']} @{fresh['owner'] or '无主'}）"), 0
        cur = conn.execute(
            "INSERT INTO checkpoints(task_id,agent,ts,kind,content) VALUES(?,?,?,?,?)",
            (tid, who, _now(), kind, content[:2000]))
        cid = cur.lastrowid
        _journal_row(conn, who, "任务检查点", f"tasks#{tid}", f"{kind}｜{content[:80]}")
        return "", cid


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


def checkpoint_count(root: str, task_id) -> int:
    """某任务检查点总数（R273）：时间线有 LIMIT，抬头不能把取到的条数当总数。"""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return 0
    with db_conn(root) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE task_id=?", (tid,)).fetchone()[0])


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
SENTRY_CHECKS = ("runtime_health", "db_health", "backup_health", "rules_health",
                 "dup_mem", "inbox", "stalled")


def sentry_sweep(root: str, cooldown_hours: int = 24, checks=None) -> tuple:
    """M5 哨兵：跑关键体检项，把 gap 聚合成一条**广播 alert 信号**（所有 agent 开工即见，
    不用各自跑体检才知道大脑有情况）。返回 (摘要文本, 信号 id 或 0——0=无可报/去重跳过)。

    去重：与最近一条 system 广播 alert 内容相同且不足 cooldown_hours 不重复发（防刷屏）。
    R8 起每条 gap 附「修复：…」——广播不是告警终点，收到的人要知道下一步做什么。
    检查项本身炸了记成一条问题（体检器故障也不静默），不拖垮整轮。"""
    import ops  # noqa: PLC0415  延迟导入：ops 反向依赖 brain，模块级导入会成环
    report = ops.run_patrol(root, checks or SENTRY_CHECKS)
    gaps = [f"· {i['name']}：{(i.get('summary') or '')[:80]}\n  修复：{i['fix'][:100]}"
            for i in report["issues"]]
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
        # R273：截断留省略号——半句话读起来像错字，且看不出是被截的
        for r in conn.execute(
                "SELECT id, CASE WHEN length(title)>60 THEN substr(title,1,60)||'…' "
                "ELSE title END AS title, "
                "CASE WHEN length(content)>80 THEN substr(content,1,80)||'…' "
                "ELSE content END AS content, "
                "project, agent, created FROM records WHERE status='quarantine' "
                "ORDER BY id DESC LIMIT ?", (max(1, min(int(limit or 50), 200)),)):
            out.append({"src": "record", **dict(r)})
        for r in conn.execute(
                "SELECT id, kind, CASE WHEN length(content)>80 THEN substr(content,1,80)||'…' "
                "ELSE content END AS content, agent, created "
                "FROM memories WHERE status='quarantine' ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit or 50), 200)),)):
            out.append({"src": "memory", **dict(r)})
    out.sort(key=lambda x: x["id"], reverse=True)
    return out[:max(1, min(int(limit or 50), 200))]


def quarantine_count(root: str) -> int:
    """隔离区总数（R273）：记录+记忆两张表合并计数，列表抬头用它报总数。"""
    with db_conn(root) as conn:
        return int(conn.execute(
            "SELECT (SELECT COUNT(*) FROM records WHERE status='quarantine')"
            " + (SELECT COUNT(*) FROM memories WHERE status='quarantine')").fetchone()[0])


# ---------------------------------------------------------------- 决策回路（M11）

REVIEW_VERDICTS = ("right", "wrong", "partial")


def decision_review_due(root: str, limit: int = 10) -> list:
    """到期未回顾的决策（M11）：review_date<=today 且 pending，附记录标题与决策段摘要。"""
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM decision_reviews WHERE status='pending' "
            "AND review_date!='' AND review_date<=date('now','localtime') "
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
                "SELECT * FROM tasks WHERE status NOT IN ('done','cancelled','dead') "
                "ORDER BY priority, id")]
        except sqlite3.Error:
            tks = []
        lines += ["", "## 未完成任务", ""]
        for t in tks:
            lines.append(f"- [#{t['id']} P{t['priority']} {t['status']}] {t['title']}"
                         f"（{t['project'] or '无项目'}）")
        path.write_text("\n".join(lines), encoding="utf-8")
        return "", str(path)


AUDIT_EXPORT_DIR = "exports"
AUDIT_EXPORT_MAX_ROWS = 200000


def audit_export(root: str, fmt: str = "jsonl", days: int = 30,
                 limit: int = 20000) -> tuple:
    """R50 审计导出：journal（含链字段）+ op_audit → JSONL/CSV，先临时文件再原子改名。

    JSONL 首行是 meta（含链校验快照），之后每行一个事件；CSV 是扁平列，链状态回执里给。
    链异常不阻止导出——出事时正是最需要把现场拉出来的时候；meta 里如实标记 ok=False。
    """
    fmt = (fmt or "jsonl").strip().lower()
    if fmt not in ("jsonl", "csv"):
        return "format 只能是 jsonl/csv", {}
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        return "days 必须是整数（0=全部历史）", {}
    if days < 0 or days > 3650:
        return f"days 只能是 0..3650（收到 {days}；0=全部）", {}
    try:
        limit = int(limit or 20000)
    except (TypeError, ValueError):
        return "limit 必须是整数", {}
    limit = max(1, min(limit, AUDIT_EXPORT_MAX_ROWS))
    since = ""
    if days:
        since = (datetime.datetime.now()
                 - datetime.timedelta(days=days)).isoformat(timespec="seconds")

    out_dir = db_path(root).parent / AUDIT_EXPORT_DIR
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"导出目录创建失败：{e}", {}
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"audit-{stamp}.{fmt}"
    n = 1
    while path.exists():
        path = out_dir / f"audit-{stamp}-{n}.{fmt}"
        n += 1

    chain = journal_verify(root)
    try:
        with db_conn(root) as conn:
            jsql = ("SELECT id, ts, agent, action, target, backup, note, prev_hash, hash "
                    "FROM journal")
            jargs = []
            if since:
                jsql += " WHERE ts>=?"
                jargs.append(since)
            jsql += " ORDER BY id ASC LIMIT ?"
            jrows = [dict(r) for r in conn.execute(jsql, (*jargs, limit))]
            osql = ("SELECT id, ts, agent, tool, project, summary, undo_kind, undo_ref "
                    "FROM op_audit")
            oargs = []
            if since:
                osql += " WHERE ts>=?"
                oargs.append(since)
            osql += " ORDER BY id ASC LIMIT ?"
            orows = [dict(r) for r in conn.execute(osql, (*oargs, limit))]
    except sqlite3.Error as e:
        return f"读取审计表失败：{e}", {}

    truncated = len(jrows) >= limit or len(orows) >= limit
    meta = {"type": "meta", "exported_at": _now(), "root": str(root), "fmt": fmt,
            "days": days, "limit": limit, "truncated": truncated,
            "journal": len(jrows), "op_audit": len(orows),
            "chain": {"ok": chain["ok"], "sealed": chain["sealed"],
                      "legacy": chain["legacy"], "breaks": chain["breaks"],
                      "head": chain["head"], "chain_at": chain["chain_at"],
                      "summary": chain["summary"]}}

    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        if fmt == "jsonl":
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(meta, ensure_ascii=False) + "\n")
                for r in jrows:
                    f.write(json.dumps({"type": "journal", **r}, ensure_ascii=False) + "\n")
                for r in orows:
                    f.write(json.dumps({"type": "op_audit", **r}, ensure_ascii=False) + "\n")
        else:
            import csv  # noqa: PLC0415 仅 CSV 路径需要
            with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["source", "id", "ts", "agent", "action", "project",
                            "target", "summary", "note", "undo", "prev_hash", "hash"])
                for r in jrows:
                    w.writerow(["journal", r["id"], r["ts"], r["agent"], r["action"], "",
                                r["target"], "", r["note"], "", r["prev_hash"], r["hash"]])
                for r in orows:
                    undo = ":".join(x for x in (r["undo_kind"], r["undo_ref"]) if x)
                    w.writerow(["op_audit", r["id"], r["ts"], r["agent"], r["tool"],
                                r["project"], "", r["summary"], "", undo, "", ""])
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return f"写导出文件失败：{e}", {}
    return "", {"path": str(path), "format": fmt, "days": days,
                "journal": len(jrows), "op_audit": len(orows),
                "truncated": truncated, "bytes": path.stat().st_size,
                "chain": chain, "meta": meta}


def _db_quick_check(conn: sqlite3.Connection) -> str:
    """返回 "" 表示完整性 ok，否则返回可读原因（不抛异常）。"""
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as e:
        return f"quick_check 执行失败：{e}"
    if not row or row[0] != "ok":
        return f"quick_check 未通过：{row[0] if row else '无结果'}"
    return ""


# R59：只认 SQLite 明确表示「文件结构坏了」的文案。锁冲突（database is locked）、
# 缺表（no such table）、打不开文件（unable to open database file）都有各自的处置路径，
# 归进「损坏」会让用户去恢复备份，反而制造数据丢失。
_DB_CORRUPT_MARKS = ("malformed", "not a database", "file is encrypted",
                     "database disk image", "database is corrupted")


def is_db_corruption(err) -> bool:
    """判断异常/文案是否属于「库文件本身损坏」。"""
    if isinstance(err, BaseException):
        if not isinstance(err, sqlite3.DatabaseError):
            return False
    msg = str(err or "").lower()
    return any(m in msg for m in _DB_CORRUPT_MARKS)


def db_corruption_report(root: str, err) -> str:
    """库损坏时的统一可行动文案（CLI / MCP 同口径，避免两套说法）。

    不写「已重试/已登记」：库都读不了，error_add 与重试都必然失败，
    告知假成功比报错本身更危险。
    """
    db = str(Path(root) / core.DIR_META / DB_NAME) if root else "<root>/_hub/brain.db"
    return (
        f"错误：大脑数据库文件已损坏（{type(err).__name__}: {err}）。\n"
        f"库文件：{db}\n"
        "所有依赖大脑的查询与写入都会失败，原地重试不会恢复。处置：\n"
        "1. 停写：关闭各 agent 的 agenthub MCP 进程与桌面端，别让进程继续写坏库。\n"
        "2. 留证：把 brain.db、brain.db-wal、brain.db-shm 一起复制到别处"
        "（-wal 里可能有未落盘数据，只留 db 会丢）。\n"
        "3. 选备份：python cli.py --root <root> call hub_export action=backups，"
        "再 action=verify path=<zip> 校验可用。\n"
        "4. 恢复：按《运维手册》§3.4 手工恢复——移走当前 db/-wal/-shm，"
        "把备份里的 brain.db 放回；别删库重建，那会清空全部记录。\n"
        "5. 复核：python cli.py --root <root> doctor 与 hub_ops_run(name=\"db_integrity\")。"
    )


def _zip_write_safe(zf, path: Path, arcname: str) -> None:
    """文件 mtime 早于 1980 时 zip 格式无法表达（from_file 抛 ValueError）。
    用固定时间戳流式写入，不让一个坏时间戳毁掉整份备份。"""
    import shutil
    import zipfile
    try:
        zf.write(path, arcname=arcname)
    except ValueError:
        zi = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_DEFLATED
        with open(path, "rb") as src, zf.open(zi, "w") as dst:
            shutil.copyfileobj(src, dst)


BACKUP_MANIFEST = "_AgentHub_备份清单.txt"
BACKUP_MAX_FILE_MB = int(os.environ.get("AGENTHUB_BACKUP_MAX_FILE_MB", "50") or 0)


def _backup_skip_dir(name: str) -> bool:
    """构建产物/缓存/依赖目录不进整包（可重建，且常占几个 GB）。"""
    n = name.lower()
    return (n in {"backup", "brain-backups", "locks", "__pycache__", ".git",
                  "node_modules", ".venv", "venv", ".next", ".pytest_cache",
                  ".mypy_cache", "target"}
            or n == "build" or n.startswith("build_")
            or n == "dist" or n.startswith("dist_"))


def backup_db(root: str, dest: str = "", max_file_mb=None) -> tuple:
    """X3 备份：① 先走 sqlite3 官方 backup API 取**含 WAL 的一致性库快照**（**不用
    shutil 复制文件**——WAL 未 checkpoint 时 copy 出的库会丢数据，2026-10-12 实测假备份）；
    ② 对快照跑 quick_check，坏快照不产出备份；③ 打包成单个 .zip：库里 + 磁盘项目文件树
    （排除构建/缓存/依赖/备份目录与超过 max_file_mb 的单文件，默认 50MB，0=不限；
    跳过项写进包内清单，还原时不静默缺件），**先写临时 zip 再原子改名**，半成品不会以
    正式备份名出现。单一还原单元，避免「只备库、还原丢源文件」。dest 可指定目录
    （不存在则建）。返回 (错误或 "", zip 路径)。旧 .db 备份仍可被 backup_verify 识别。"""
    import zipfile
    src = db_path(root)
    if not src.exists():
        return "源库不存在", ""
    cap_mb = BACKUP_MAX_FILE_MB if max_file_mb is None else max(0, int(max_file_mb))
    cap_bytes = cap_mb * 1024 * 1024
    out_dir = Path(dest).expanduser() if dest else db_path(root).parent / "backup"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"备份目录创建失败：{e}", ""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_path = out_dir / f"brain-{stamp}.zip"
    n = 1
    while zip_path.exists():
        zip_path = out_dir / f"brain-{stamp}-{n}.zip"
        n += 1
    tmp = out_dir / f".tmp-brain-{stamp}-{os.getpid()}-{time.time_ns()}.db"
    tmp_zip = out_dir / f".tmp-brain-{stamp}-{os.getpid()}-{time.time_ns()}.zip"
    s_conn = d_conn = None
    try:
        # ① 一致性库快照（含 WAL）
        s_conn = sqlite3.connect(str(src), timeout=5)
        d_conn = sqlite3.connect(str(tmp), timeout=5)
        s_conn.backup(d_conn)
        # ② 快照完整性：坏快照宁可不备份，也不产出「看起来成功」的损坏包
        bad = _db_quick_check(d_conn)
        if bad:
            d_conn.close()
            d_conn = None
            s_conn.close()
            s_conn = None
            tmp.unlink(missing_ok=True)
            return f"备份失败：库快照完整性校验未通过（{bad}）", ""
        d_conn.close()
        d_conn = None
        s_conn.close()
        s_conn = None
        # ③ 打包：库快照 + 磁盘文件树
        root_p = Path(root)
        EXCLUDE_FILES = {src.name, src.name + "-wal", src.name + "-shm", tmp.name}
        skipped_dirs = set()
        skipped_files = []
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(tmp, arcname="brain.db")  # 一致库放根
            for p in root_p.rglob("*"):
                if p.is_dir():
                    continue
                rel = p.relative_to(root_p)
                hit = next((part for part in rel.parts[:-1] if _backup_skip_dir(part)), "")
                if hit:
                    idx = rel.parts.index(hit)
                    skipped_dirs.add("/".join(rel.parts[:idx + 1]))
                    continue
                if rel.name in EXCLUDE_FILES or rel.name.startswith("brain.db.bak-"):
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                if cap_bytes and size > cap_bytes:
                    skipped_files.append((str(rel).replace("\\", "/"), size))
                    continue
                _zip_write_safe(zf, p, str(rel).replace("\\", "/"))
            cap_note = f"单文件 > {cap_mb}MB" if cap_mb else "未设上限"
            lines = ["# AgentHub 备份清单",
                     f"# 生成时间: {stamp}",
                     f"# 跳过文件: {len(skipped_files)}（{cap_note}）",
                     f"# 跳过目录: {len(skipped_dirs)}（构建/缓存/依赖/备份）"]
            for d in sorted(skipped_dirs):
                lines.append(f"skipped_dir\t{d}")
            for rel_path, size in skipped_files[:500]:
                lines.append(f"skipped_file\t{rel_path}\t{size / 1048576:.1f}MB")
            if len(skipped_files) > 500:
                lines.append(f"# 其余 {len(skipped_files) - 500} 个超限文件省略")
            zf.writestr(BACKUP_MANIFEST, "\n".join(lines) + "\n")
        os.replace(tmp_zip, zip_path)  # 同盘原子落名：要么完整备份，要么没有
        tmp.unlink(missing_ok=True)
    except (sqlite3.Error, OSError, zipfile.BadZipFile, ValueError) as e:
        for c in (d_conn, s_conn):
            try:
                if c:
                    c.close()
            except sqlite3.Error:
                pass
        for leftover in (tmp, tmp_zip):
            try:
                leftover.unlink(missing_ok=True)
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
    """X3 校验备份：只读打开 + quick_check 完整性 + 关键表计数（不写、不恢复——恢复是
    人的手工动作）。支持 .zip（库+文件，逐项校验并报告文件数）与旧 .db（仅库）。
    坏 zip / 坏库都返回错误文案，不向调用方抛异常。返回 (错误或 "", 摘要)。"""
    import zipfile
    import tempfile
    p = Path(path).expanduser()
    if not p.is_file():
        return f"备份文件不存在：{p}", ""
    try:
        if p.suffix == ".zip":
            with zipfile.ZipFile(p) as zf:
                bad_member = zf.testzip()
                if bad_member is not None:
                    return f"压缩包损坏（{bad_member} CRC 校验失败）", ""
                names = zf.namelist()
                if "brain.db" not in names:
                    return "备份缺少 brain.db（不是有效的 AgentHub 备份）", ""
                manifest = (zf.read(BACKUP_MANIFEST).decode("utf-8", "replace")
                            if BACKUP_MANIFEST in names else "")
                data = zf.read("brain.db")
            tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tf.write(data)
            tf.close()
            conn = None
            try:
                conn = sqlite3.connect(f"file:{tf.name}?mode=ro", uri=True, timeout=5)
                bad = _db_quick_check(conn)
                if bad:
                    return f"备份库完整性校验未通过（{bad}）", ""
                got = {}
                for tbl in ("records", "memories", "projects"):
                    try:
                        got[tbl] = conn.execute(
                            f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                    except sqlite3.Error:
                        got[tbl] = -1
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                try:
                    Path(tf.name).unlink(missing_ok=True)
                except OSError:
                    pass
            n_files = sum(1 for n in names if n not in ("brain.db", BACKUP_MANIFEST))
            skip_note = ""
            m = re.search(r"# 跳过文件: (\d+).*?# 跳过目录: (\d+)", manifest, re.S)
            if m:
                skip_note = f" · 跳过 {m.group(1)} 文件/{m.group(2)} 目录（见清单）"
            return "", ("备份可读：DB(" + " · ".join(f"{k} {v}" for k, v in got.items())
                        + f") · 文件 {n_files} 个 · 完整性 ok" + skip_note)
        # 旧 .db 备份（仅库）
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            bad = _db_quick_check(conn)
            if bad:
                return f"备份库完整性校验未通过（{bad}）", ""
            got = {}
            for tbl in ("records", "memories", "projects"):
                try:
                    got[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                except sqlite3.Error:
                    got[tbl] = -1
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    except (sqlite3.Error, zipfile.BadZipFile, OSError, EOFError, RuntimeError) as e:
        return f"读取备份失败：{e}", ""
    return "", "备份可读：" + " · ".join(
        f"{k} {v}" for k, v in got.items()) + " · 完整性 ok"


_RESTORE_CORE_TABLES = ("records", "memories", "projects", "tasks")
_RESTORE_COUNT_TABLES = _RESTORE_CORE_TABLES + ("journal", "signals", "checkpoints")


def _journal_newest(conn) -> str:
    try:
        return str(conn.execute("SELECT MAX(ts) FROM journal").fetchone()[0] or "")
    except sqlite3.Error:
        return ""


def _ts_age_note(ts: str) -> str:
    """把 ISO 时间写成「N 小时前」；解析不了就原样返回。"""
    if not ts:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(ts[:19], fmt)
        except ValueError:
            continue
        mins = int((datetime.datetime.now() - dt).total_seconds() // 60)
        if mins < 0:
            return f"{ts}（时间戳在未来？）"
        if mins < 60:
            return f"{ts}（{mins} 分钟前）"
        if mins < 48 * 60:
            return f"{ts}（{mins // 60} 小时前）"
        return f"{ts}（{mins // 1440} 天前）"
    return ts


def backup_drill(root: str, path: str = "") -> tuple:
    """R61 恢复演练（R75 起留审计痕）：把备份里的 brain.db 复制到临时目录、按恢复流程打开，
    校验 quick_check / 核心表 / schema 版本 / 数据量与时效，并与现场库只读对比。
    不写现场数据、不碰 -wal/-shm、不自动恢复——恢复仍是人的手工动作。R75 起成功后
    在 journal 留一条「备份演练」痕（旁路写，失败不影响结论；「可恢复性上次被证明
    是什么时候」从此可审计）。
    path 留空取最新整包（zip）；没有整包才回退到最新备份。"""
    import shutil
    import tempfile
    import zipfile
    p = Path(path).expanduser() if str(path or "").strip() else None
    pick_note = ""
    if p is None:
        inv = backup_inventory(root)
        items = ((inv["families"].get("zip") or {}).get("items") or [])
        newest = items[0]["path"] if items else ((inv.get("newest") or {}).get("path") or "")
        if not newest:
            return "还没有备份：先 hub_export action=backup 备一份再演练", ""
        p = Path(newest)
        pick_note = "（未指定 path，取最新整包）" if items else "（未指定 path，无整包，取最新备份）"
    if not p.is_file():
        return f"备份文件不存在：{p}", ""
    drill_dir = tempfile.mkdtemp(prefix="agenthub-drill-")
    try:
        db_copy = Path(drill_dir) / "brain.db"
        file_note = ""
        n_files = 0
        unpacked = 0
        if p.suffix == ".zip":
            try:
                with zipfile.ZipFile(p) as zf:
                    bad_member = zf.testzip()
                    if bad_member is not None:
                        return f"压缩包损坏（{bad_member} CRC 校验失败），不能用于恢复", ""
                    infos = zf.infolist()
                    if "brain.db" not in zf.namelist():
                        return "备份缺少 brain.db（不是有效的 AgentHub 备份）", ""
                    n_files = sum(1 for i in infos
                                  if i.filename not in ("brain.db", BACKUP_MANIFEST)
                                  and not i.is_dir())
                    unpacked = sum(i.file_size for i in infos
                                   if i.filename not in ("brain.db", BACKUP_MANIFEST))
                    zf.extract("brain.db", drill_dir)
            except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as e:
                return f"备份读取失败：{e}", ""
            file_note = f"文件树 {n_files} 个（解压后约 {unpacked / 1048576:.1f}MB）"
        else:
            try:
                shutil.copy2(p, db_copy)
            except OSError as e:
                return f"备份读取失败：{e}", ""
            file_note = "仅库备份（旧版 .db），不含项目文件树——恢复只覆盖大脑库"
        if not db_copy.is_file():
            return "备份中未能取出 brain.db", ""
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True, timeout=5)
            bad = _db_quick_check(conn)
            if bad:
                return f"备份库完整性校验未通过（{bad}），不能用于恢复", ""
            tabs = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in _RESTORE_CORE_TABLES if t not in tabs]
            if missing:
                return f"备份缺少核心表 {', '.join(missing)}（不是有效的 AgentHub 库）", ""
            counts = {}
            for t in _RESTORE_COUNT_TABLES:
                try:
                    counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.Error:
                    counts[t] = -1
            db_v = None
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'").fetchone()
                db_v = int(str(row[0]).strip()) if row and str(row[0]).strip() else None
            except (sqlite3.Error, TypeError, ValueError):
                db_v = None
            newest_ts = _journal_newest(conn)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        if db_v is None:
            schema_note = "schema 未登记（旧版备份，恢复后由当前代码补表）"
        elif db_v > SCHEMA_VERSION:
            schema_note = (f"schema v{db_v} 比当前代码 v{SCHEMA_VERSION} 更新——"
                           f"须先升级代码再恢复，旧代码读新表会缺列")
        elif db_v < SCHEMA_VERSION:
            schema_note = f"schema v{db_v} 低于当前 v{SCHEMA_VERSION}（恢复后首次打开自动迁移）"
        else:
            schema_note = f"schema v{db_v}（与当前一致）"
        # 现场库只读对比：备份有多旧、恢复会丢多少写入
        live_counts: dict = {}
        live_ts = ""
        live_note = ""
        try:
            lconn = sqlite3.connect(f"file:{db_path(root)}?mode=ro", uri=True, timeout=5)
            try:
                for t in _RESTORE_CORE_TABLES:
                    try:
                        live_counts[t] = lconn.execute(
                            f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    except sqlite3.Error:
                        live_counts[t] = -1
                live_ts = _journal_newest(lconn)
            finally:
                lconn.close()
        except sqlite3.Error as e:
            live_note = ("现场库读不了（" + ("可能已损坏" if is_db_corruption(e)
                                             else type(e).__name__)
                         + "）：本次只报备份自身状态")
        lines = [f"恢复演练：可用{pick_note}",
                 f"备份：{p}（{p.stat().st_size / 1048576:.1f}MB，"
                 f"{datetime.datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}）",
                 f"库：quick_check ok · {schema_note} · {len(tabs)} 张表",
                 "数据：" + " · ".join(f"{t} {counts[t]}" for t in _RESTORE_COUNT_TABLES
                                       if counts[t] >= 0),
                 f"文件树：{file_note}"]
        if newest_ts:
            lines.append(f"备份时效：包内最新流水 {_ts_age_note(newest_ts)}")
        diff = [f"{t} {live_counts[t] - counts[t]:+d}"
                for t in _RESTORE_CORE_TABLES
                if live_counts.get(t, -1) >= 0 and counts[t] >= 0
                and live_counts[t] != counts[t]]
        if live_ts and newest_ts and live_ts > newest_ts:
            lines.append(f"⚠ 现场比备份新（现场流水 {live_ts[:19]}）——直接恢复会丢掉备份之后的写入"
                         + (f"（现场-备份：{' · '.join(diff)}）" if diff else ""))
        elif diff:
            lines.append(f"注意：现场与备份数据量不同（现场-备份：{' · '.join(diff)}）"
                         "——恢复会回到备份时刻的状态")
        elif live_ts and newest_ts:
            lines.append(f"现场流水 {live_ts[:19]}，不晚于备份")
        if live_note:
            lines.append(f"提示：{live_note}")
        need_mb = unpacked / 1048576 + db_copy.stat().st_size / 1048576
        try:
            du = shutil.disk_usage(str(db_path(root).parent))
            free_mb = du.free / 1048576
            space = f"约需 {need_mb:.1f}MB，目标盘剩余 {free_mb / 1024:.1f}GB"
            if free_mb < need_mb:
                space += "（⚠ 空间不足）"
        except OSError:
            space = f"约需 {need_mb:.1f}MB（磁盘可用空间未取到）"
        lines.append(f"空间：{space}")
        lines.append("结论：可用于恢复（演练不写现场数据（仅审计流水留痕））。恢复：停写 → 现场 "
                     "db/-wal/-shm 留证移走 → 解压 brain.db 放回 _hub → doctor 与 "
                     "db_integrity 复核（详见运维手册 §3.4）。")
        # R75：演练留痕——「可恢复性上次被证明是什么时候」是审计要回答的问题，
        # 没痕迹就等于没练过（备份不验恢复等于没备份）。旁路写，失败不影响结论。
        journal_add(root, "system", "备份演练", str(p),
                    note="结论=可用于恢复（未写现场数据）")
        return "", "\n".join(lines)
    finally:
        shutil.rmtree(drill_dir, ignore_errors=True)


# R75：恢复演练的提醒周期（天）。本地单人系统，演练一次约十几秒+1GB 临时空间，
# 30 天既不打断节奏也不至于「装了 drill 却一年想不起跑」。
DRILL_REMIND_DAYS = 30


def backup_drill_due(root: str, max_days: int = DRILL_REMIND_DAYS) -> dict:
    """R75：距上次成功演练是否已超期（只读）。返回 {due, age_days, last_ts, reason}。

    备份不验恢复等于没备份（生产共识）——R61 建了 drill 能力，但没有任何东西会
    主动说「该跑一次了」；本函数给心跳推送做**条件式**提醒：从未演练过（有备份）
    或上次演练超 max_days 天 → due=True。无备份/读失败/时间不可解析 → 不猜（due=False）。
    """
    try:
        inv = backup_inventory(root)
        if not inv.get("total_count"):
            return {"due": False, "age_days": None, "last_ts": "", "reason": "无备份"}
        with db_conn(root) as conn:
            row = conn.execute(
                "SELECT ts FROM journal WHERE action=? ORDER BY id DESC LIMIT 1",
                ("备份演练",)).fetchone()
        if not row:
            return {"due": True, "age_days": None, "last_ts": "", "reason": "从未演练"}
        last = str(row["ts"] or "")
        try:
            age = (datetime.datetime.now() - datetime.datetime.fromisoformat(last)).days
        except (TypeError, ValueError):
            return {"due": False, "age_days": None, "last_ts": last, "reason": "时间不可解析"}
        return {"due": age >= max_days, "age_days": age, "last_ts": last, "reason": ""}
    except Exception as e:  # noqa: BLE001  提醒是旁路，失败不出声
        return {"due": False, "age_days": None, "last_ts": "", "reason": type(e).__name__}


# ---------------------------------------------------------------- 时间机器（X4）

def history_of(root: str, kind: str = "record", oid=0) -> dict:
    """X4 时间机器：某条记录/记忆/任务的完整变更史（多源合并，按时间升序）。
    记录/记忆：journal 流水 + supersede 链 + 自身关键时点；
    任务（R5）：journal 流水 + 检查点时间线 + 任务卡关键时点（创建/租约/完成/依赖/截止）。
    任务另返回 clocks（R27）：推进/检查点/心跳/租约四钟分列，updated 只作「最后改动」，
    即使被心跳续租刷新也不会冒充推进。
    返回 {"kind","id","item","timeline":[{"ts","source","event","detail"}],"clocks"}。"""
    kind = (kind or "record").strip()
    if kind not in ("record", "memory", "task"):
        return {"kind": kind, "id": oid, "item": None, "timeline": []}
    try:
        oid = int(oid)
    except (TypeError, ValueError):
        return {"kind": kind, "id": oid, "item": None, "timeline": []}
    table = {"record": "records", "memory": "memories", "task": "tasks"}[kind]
    mark = table
    tl: list = []
    clocks: dict = {}
    with db_conn(root) as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (oid,)).fetchone()
        item = dict(row) if row else None
        if not item:
            return {"kind": kind, "id": oid, "item": None, "timeline": []}
        # ① journal 流水（target 两种写法都认：records#N 与 brain:records#N；
        # LIKE 只是粗筛，末尾必须精确匹配 id——否则查 #1 会混入 #10/#11 的流水）
        for r in conn.execute(
                "SELECT ts, action, agent, target, note FROM journal WHERE target LIKE ? "
                "ORDER BY id", (f"%{mark}#{oid}",)):
            if not re.search(rf"(?:^|:){re.escape(mark)}#{oid}$", r["target"] or ""):
                continue
            tl.append({"ts": r["ts"], "source": "journal",
                       "event": r["action"], "detail": (r["note"] or "")[:120]})
        if kind == "task":
            now = datetime.datetime.now()
            cf = _task_clock_fields(item, _checkpoint_index(conn),
                                    _sessions_index(conn), now)
            lease_state, lease_left = _task_lease(item, _claim_cutoff(), now)
            clocks = {"progress_at": (item.get("progress_at") or "").strip(),
                      "progress_age_h": cf["_progress_age_h"],
                      "stalled": cf["_progress_stall"],
                      "last_checkpoint": cf["_last_cp"],
                      "last_checkpoint_age_h": cf["_last_cp_age_h"],
                      "heartbeat_at": cf["_heartbeat_at"],
                      "heartbeat_age_h": cf["_heartbeat_age_h"],
                      "lease_state": lease_state, "lease_left_h": lease_left}
            # ② 检查点时间线（断点续跑视角的进展史）
            for r in conn.execute(
                    "SELECT ts, agent, kind, content FROM checkpoints WHERE task_id=? "
                    "ORDER BY id", (oid,)):
                tl.append({"ts": r["ts"], "source": "checkpoint",
                           "event": f"检查点（{r['kind']}）",
                           "detail": f"{r['agent']}：{r['content'][:100]}"})
            # ③ 任务卡关键时点
            if item.get("created"):
                tl.append({"ts": item["created"], "source": "item", "event": "创建",
                           "detail": f"by {item.get('created_by') or '未署名'}"
                                     f"｜{item.get('project') or '无项目'}"})
            if item.get("claimed_at"):
                tl.append({"ts": item["claimed_at"], "source": "item", "event": "当前租约",
                           "detail": f"@{item.get('owner') or '无主'}"
                                     f"（{CLAIM_TTL_HOURS}h 无心跳/更新可接管）"})
            if item.get("done_at"):
                tl.append({"ts": item["done_at"], "source": "item", "event": "完成",
                           "detail": f"@{item.get('owner') or '未署名'}"})
            if item.get("updated") and item.get("updated") != item.get("created"):
                tl.append({"ts": item["updated"], "source": "item", "event": "最后改动",
                           "detail": f"status={TASK_CN.get(item['status'], item['status'])}"
                                     "（心跳续租也会刷新，不代表进展）"})
            pa = (item.get("progress_at") or "").strip()
            if pa and pa != (item.get("updated") or "").strip():
                tl.append({"ts": pa, "source": "item", "event": "最后推进",
                           "detail": "最后一条真实任务动作（认领/流转/改详情/写检查点）"})
            if (item.get("deps") or "").strip():
                tl.append({"ts": item.get("created") or "", "source": "item",
                           "event": "前置链", "detail": item["deps"]})
            if (item.get("due") or "").strip():
                tl.append({"ts": item.get("created") or "", "source": "item",
                           "event": "截止日", "detail": item["due"]})
        else:
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
    return {"kind": kind, "id": oid, "item": item, "timeline": tl, "clocks": clocks}


def heartbeat_touch(root: str, agent: str, project: str = "", note: str = "") -> tuple:
    """更新心跳（只剪超出 SESSION_RETAIN 的会话行 + UPSERT 本人，绝不整表重建），
    返回 (错误或"", 同项目活跃会话)。

    注意「剪枝窗口 ≠ 在线窗口」：剪枝按 SESSION_RETAIN（24h，服务展示），
    返回的 active 按 SESSION_ACTIVE（5 分钟，同项目正在工作的并发判定）——两者互不影响。"""
    agent = _norm_agent(agent)
    if not agent:
        return "agent 必填", []
    now = datetime.datetime.now()
    now_s = now.isoformat(timespec="seconds")
    retain_cutoff = (now - datetime.timedelta(seconds=SESSION_RETAIN)) \
        .isoformat(timespec="seconds")
    active_cutoff = (now - datetime.timedelta(seconds=SESSION_ACTIVE)) \
        .isoformat(timespec="seconds")
    with db_conn(root) as conn:
        # 只剪自己之外的过期行，绝不 DELETE 全表再重建：两个 agent 并发心跳时，
        # 「先读快照 → 后写重建」会丢掉另一方刚写进去的会话（Windows/SQLite 下还可能直接锁冲突）。
        # 2026-10-11 之前用 SESSION_TTL（30 分钟）剪枝还会让展示层拿不到更早数据；
        # 保留窗口与在线判定必须分开，这里用 SESSION_RETAIN 剪、SESSION_ACTIVE 判在线。
        conn.execute("DELETE FROM sessions WHERE ts < ?", (retain_cutoff,))
        conn.execute(
            "INSERT INTO sessions(agent,project,note,ts) VALUES(?,?,?,?) "
            "ON CONFLICT(agent) DO UPDATE SET project=excluded.project, "
            "note=excluded.note, ts=excluded.ts",
            (agent.strip(), project or "", note or "", now_s))
        # 心跳即续租：agent 还在干活就不该因 2h 没写任务而被别人接管。
        # 有项目时只续该项目下的任务，避免跨项目误续；无项目时续本人全部在办任务。
        if project:
            conn.execute(
                "UPDATE tasks SET claimed_at=?, updated=? "
                "WHERE status='in_progress' AND owner=? AND project=?",
                (now_s, now_s, agent, project))
        else:
            conn.execute(
                "UPDATE tasks SET claimed_at=?, updated=? "
                "WHERE status='in_progress' AND owner=?",
                (now_s, now_s, agent))
        _upsert_agent(conn, agent, project, beat=True)
        active = [dict(r) for r in conn.execute(
            "SELECT agent,project,note,ts FROM sessions "
            "WHERE project=? AND agent<>? AND ts>=? ORDER BY ts DESC",
            (project, agent, active_cutoff))] if project else []
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
    """注册 agent 全表 + 在线状态（sessions 存活者打 live 标），按最近活跃排序。

    并列（同秒注册/同一批种子数据）按 name 升序兜底：SQLite 对 ORDER BY 并列行
    不保证顺序，曾让下拉框选中值在 zcode/workbuddy 之间漂移，结构快照基线随之抖动。
    """
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM agents ORDER BY last_seen DESC, name ASC")]
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
        out["handoffs"] = handoff_annotate([dict(r) for r in conn.execute(
            "SELECT id, project, agent, status, created FROM handoffs WHERE status='open' "
            "ORDER BY id DESC LIMIT 5")], now)
        out["handoffs_total"] = conn.execute(
            "SELECT COUNT(*) FROM handoffs WHERE status='open'").fetchone()[0]
        out["pinned"] = [dict(r) for r in conn.execute(
            "SELECT id, kind, substr(content,1,50) AS content FROM memories "
            "WHERE status='active' AND pinned=1 ORDER BY id LIMIT 5")]
        out["stalled"] = [r["name"] for r in conn.execute(
            "SELECT name FROM projects WHERE status='stalled' ORDER BY name LIMIT 5")]
        out["stalled_total"] = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE status='stalled'").fetchone()[0]
        # 过程可见性（2026-10-21）：不只知道谁在线，还要知道在办什么、哪条租约快过期/可接管。
        # 只读查询，不写 searches，保持 status 的高频调用不污染读写比口径。
        out["tasks_open_total"] = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','cancelled','dead')")             .fetchone()[0]
        out["tasks_in_progress"] = [dict(r) for r in conn.execute(
            "SELECT id,project,title,owner,claimed_at,due,priority FROM tasks "
            "WHERE status='in_progress' ORDER BY priority, id LIMIT 10")]
        cutoff = _claim_cutoff()
        # R2：无主（owner 空）与租约缺失（claimed_at 空）同样属于「可接管」，
        # 不能只按过期时间过滤——那样这些任务在状态面上完全隐身。
        stale_claims = []
        for r in conn.execute(
                "SELECT id,project,title,owner,claimed_at FROM tasks "
                "WHERE status='in_progress' "
                "AND (owner='' OR claimed_at='' OR claimed_at < ?) "
                "ORDER BY claimed_at, id LIMIT 10", (cutoff,)):
            d = dict(r)
            if not (d["owner"] or "").strip():
                d["reason"] = "无主"
            elif not (d["claimed_at"] or "").strip():
                d["reason"] = "租约缺失"
            else:
                d["reason"] = "租约过期"
            stale_claims.append(d)
        out["stale_claims"] = stale_claims
        out["claim_ttl_hours"] = CLAIM_TTL_HOURS
        # R98：审批积压也是「过程可见性」的一部分——只在专门队列里可见，日常状态页
        # 就会漏掉「有人在等拍板」。口径与 task_approvals 一致（等最久优先）。
        out["approvals"] = [dict(r) for r in conn.execute(
            "SELECT id,project,title,owner,claimed_at FROM tasks "
            "WHERE status='in_progress' AND approval_required=1 "
            "AND COALESCE(approved_by,'')='' "
            "ORDER BY CASE WHEN COALESCE(claimed_at,'')='' THEN 1 ELSE 0 END, "
            "claimed_at ASC, id ASC LIMIT 5")]
        out["approvals_total"] = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='in_progress' AND approval_required=1 "
            "AND COALESCE(approved_by,'')=''").fetchone()[0]
        for t in out["approvals"]:
            wait_h = _age_hours((t.get("claimed_at") or "").strip(), now)
            t["_approval_wait_h"] = wait_h
            t["_approval_aging"] = bool(wait_h is not None
                                        and wait_h >= TASK_APPROVAL_AGING_H)
    out["todos"] = extract_todos(root, limit=5)
    return out


# ---------------------------------------------------------------- 备份

# 备份家族（R7）：一处定义「有哪些备份、默认留几份」，盘点/清理/体检共用，
# 防三处各写一套 glob 后口径漂移（漏掉的家族永远不会被盘点，也就永远不会被清理）。
# key: (相对 _hub 的子目录, 文件名模式, 默认保留份数, 人读说明)
BACKUP_FAMILIES = {
    "zip": ("backup", "brain-*.zip", 3, "库+文件树整包（唯一可整盘还原的备份）"),
    "legacy_db": ("backup", "brain-*.db", 1, "旧版仅库备份（不含源文件）"),
    "snapshot": (BACKUP_DIR, "brain-*.db", BACKUP_KEEP, "在线库快照（backup_brain 自动轮转）"),
    "manual_bak": (".", "brain.db.bak-*", 2, "手工改动前的库快照"),
}


def backup_inventory(root: str) -> dict:
    """R7 备份盘点：四个家族逐项列出（路径/大小/年龄），按时间新在前。
    只读、不判断能不能删——删不删由 backup_prune 按保留策略算。"""
    hub = db_path(root).parent
    now = time.time()
    fams: dict = {}
    all_items: list = []
    for key, (sub, pat, keep, label) in BACKUP_FAMILIES.items():
        d = hub / sub
        items = []
        try:
            for p in d.glob(pat):
                if not p.is_file():
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                item = {"path": str(p), "size": st.st_size,
                        "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        "mtime_ts": st.st_mtime,
                        "age_days": max(0, int((now - st.st_mtime) // 86400))}
                items.append(item)
        except OSError:
            items = []
        items.sort(key=lambda x: (x["mtime_ts"], x["path"]), reverse=True)
        fams[key] = {"label": label, "dir": str(d), "keep_default": keep,
                     "count": len(items), "bytes": sum(i["size"] for i in items),
                     "items": items}
        all_items.extend(items)
    newest = max(all_items, key=lambda x: x["mtime_ts"], default=None)
    oldest = min(all_items, key=lambda x: x["mtime_ts"], default=None)
    return {"hub": str(hub), "families": fams,
            "total_count": len(all_items),
            "total_bytes": sum(i["size"] for i in all_items),
            "newest": newest, "oldest": oldest}


def _backup_prune_safe(p: Path, hub: Path) -> bool:
    """清理路径安全闸：必须落在 _hub 内、必须是文件、绝不碰主库与 WAL/SHM。"""
    try:
        rp = p.resolve()
        rp.relative_to(hub)
    except (OSError, ValueError):
        return False
    return rp.is_file() and rp.name not in ("brain.db", "brain.db-wal", "brain.db-shm")


def backup_prune(root: str, keep_zip: int = 3, keep_snapshot: int = 10,
                 keep_legacy: int = 1, keep_bak: int = 2, min_age_days: int = 7,
                 apply: bool = False, verify_first: bool = True) -> dict:
    """R7 按家族保留策略清理备份：默认**只预演**（apply=False），apply=True 才真删。
    两道保护：① 每家族至少保留最新 N 份；② 未满 min_age_days 的一律不动（防刚备份完就被清）。
    只删 BACKUP_FAMILIES 模式命中的文件，主库/WAL/SHM 无论如何不碰；删除写 journal 可追溯。

    R258 第三道保护（verify_first）：真删之前**先证明留下的那份还能还原**——验最新整包
    （没整包就验最新快照），校验失败一律拒绝删除。动机：本机体检报「可清理 1750MB」，
    但一直没人按——不是不想清，是不知道清完还能不能回去。不可逆动作前面先放一道可验证
    的证据，比在文档里写十遍「确认后再删」有用。校验只读（zip CRC + quick_check + 计数），
    不写现场数据。想跳过（例如已知那份坏了、就是要清）传 verify_first=False。
    """
    inv = backup_inventory(root)
    hub = Path(inv["hub"]).resolve()
    keeps = {"zip": keep_zip, "snapshot": keep_snapshot,
             "legacy_db": keep_legacy, "manual_bak": keep_bak}
    cands: list = []
    for key, fam in inv["families"].items():
        keep = max(0, int(keeps.get(key, fam["keep_default"])))
        for i, item in enumerate(fam["items"]):
            if i < keep or item["age_days"] < min_age_days:
                continue
            cands.append({**item, "family": key})
    out = {"ok": True, "apply": bool(apply), "candidates": cands,
           "candidate_bytes": sum(c["size"] for c in cands),
           "deleted": [], "reclaimed_bytes": 0, "errors": [],
           "inventory": inv}
    if not apply:
        return out
    if verify_first and cands:
        # 留下的还原点按「整包 > 快照 > 旧库备份 > 手工快照」挑一份来验
        target = None
        for key in ("zip", "snapshot", "legacy_db", "manual_bak"):
            fam = inv["families"].get(key) or {}
            if fam.get("items"):
                target = fam["items"][0]
                break
        if target:
            err, summary = backup_verify(root, target["path"])
            out["verify"] = {"path": target["path"], "ok": not err,
                             "summary": summary if not err else err}
            if err:
                out["ok"] = False
                out["blocked"] = f"清理前校验失败，已拒绝删除：{err}"
                return out
    reclaimed = 0
    for c in cands:
        p = Path(c["path"])
        if not _backup_prune_safe(p, hub):
            out["errors"].append(f"跳过不安全路径：{p}")
            continue
        try:
            size = p.stat().st_size
            p.unlink()
            out["deleted"].append(c)
            reclaimed += size
        except OSError as e:
            out["errors"].append(f"{p.name}：{e}")
    out["reclaimed_bytes"] = reclaimed
    out["ok"] = not out["errors"]
    if out["deleted"]:
        journal_add(root, "mcp-server", "backup_prune", "backup",
                    note=f"清理 {len(out['deleted'])} 份，释放 {reclaimed // 1024}KB"
                     f"（keep zip={keep_zip} snapshot={keep_snapshot} "
                     f"legacy={keep_legacy} bak={keep_bak}，min_age={min_age_days}d）")
    out["inventory"] = backup_inventory(root)
    return out


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
        if len(baks) > keep:
            # R256：逐份删、失败跳过——裸 unlink 遇到一份被锁（备份刚写完被杀软扫、或
            # 有人正拿它做恢复演练）就会中断整轮轮转，症状是快照数降不下来且无声。
            for old in (baks if keep <= 0 else baks[:-keep]):   # keep=0 的负零切片陷阱
                try:
                    old.unlink()
                except OSError:
                    pass
        return str(dst)
    except (sqlite3.Error, OSError) as e:
        return f"备份失败：{e}"


# ---------------------------------------------------------------- 智能层（零依赖本地算法：待办提取 / 相似检测 / 体检报告）

# 引导词必须紧贴冒号才算待办句式：否则「待办勾销闭环：」「两条'待办'系误抓」
# 这类复合词/引用全部误抓（2026-10-02 实锤），宁可漏自然语句不可滥报
_TODO_RE = re.compile(
    r"(待办事项|待办|后续|下次|下一步|TODO|待确认|待验证|待实测|需要再|记得|提醒用户|尚未完成|遗留问题|待人工|待续)"
    r"[：:]([^\n]{4,120})")


_WORD_RE = re.compile(r"\w+")


def _tokens(text: str) -> set:
    """中文 bigram + 拉丁词的轻量分词（零依赖），供相似度计算。

    R246：归一化由 `re.sub(r"[^\\w一-鿿]+", " ", lower).split()` 换成
    `_WORD_RE.findall(lower)`——两者都是「按 \\w 的极大连续段切分」，非单词串只当
    分隔符（连续多个与单个等价），对任意输入逐段全等；但 findall 一趟扫完、只分配
    命中片段，省掉整串替换再二次扫描。真实库 1493 记录 + 392 记忆 118 万字符
    202→166ms。原字符类里的 `一-鿿` 本就冗余（`\\w` 已含 CJK）。
    后半段仍是 R235 的判定：小写化后 `isascii() and isalnum()` 与 `[a-z0-9]+`
    全匹配的集合完全相同，但省掉每段一次正则调用。
    """
    toks: set = set()
    for seg in _WORD_RE.findall((text or "").lower()):
        if seg.isascii() and seg.isalnum():
            toks.add(seg)
        else:
            toks.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return toks


def _bits_index(token_sets: list):
    """把 token 集合编码成整型位集，返回 (bits, sizes)。

    R235：|A∩B| 从 `len(ta & tb)` 换成 `(a & b).bit_count()`——每个 token 独占
    一位、无哈希无近似，交集大小逐对严格相等；Python 大整数按字并行，真实库
    6.8 万对 448ms→77ms（含 sizes 预算，内层不再重复计数）。词表按本次调用现场
    构建：同一语料内映射一致即可，不依赖跨调用稳定性。
    """
    vocab = {}
    for toks in token_sets:
        for t in toks:
            if t not in vocab:
                vocab[t] = len(vocab)
    nbytes = (len(vocab) + 7) >> 3
    bits, sizes = [], []
    for toks in token_sets:
        buf = bytearray(nbytes)
        for t in toks:
            i = vocab[t]
            buf[i >> 3] |= 1 << (i & 7)
        bits.append(int.from_bytes(buf, "little"))
        sizes.append(len(toks))
    return bits, sizes


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
            "SELECT id, project, agent, date, title, content, status, "
            "superseded_by, valid_until FROM records WHERE id=?",
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


# ---------------------------------------------------------------- D5·确定性裁决建议

def _adjudicate(ma: dict, mb: dict, vers: list, quals: list) -> dict:
    """D5 核心：两条共存的相似条目，确定性给出「谁留谁退」的建议。

    外部研究给的正解模式是 **「LLM 做语义识别、确定性代码做选择」**
    （ranksquire 引 Stats：单跳 67.2→78.0）——落到本库：`similar_*` 已经用与模型无关
    的词法相似度完成了「这几条在说同一件事」的识别，「选谁」这一步必须是确定性的：
    同样输入永远同样输出，不依赖采样温度、不依赖调用次数、不依赖谁先被检索到。

    **分层规则**（关键取舍：版本信号与质量信号冲突时**不覆盖，而是降级**）：
      ① **版本信号**（调用方给的 vers 序列，从强到弱）：谁新谁是现行的。这是 ghost
         的定义决定的——多版本共存场景里，最新版本才是现行的。
      ② **质量信号**（quals，从强到弱）：只在版本信号**完全打平**时做 tiebreak。
      ③ **二者冲突**（旧的那条质量更高：三个月前带完整证据链的结论 vs 昨天写的草稿）
         → 不假装知道真相：`confident=False` 并把冲突点写进 note，交给 agent/用户。
         系统不知道外部世界何时变的，「诚实标出来」比「猜一个」强。

    参数 vers/quals 为 [(取值函数, 标签), ...]，由调用方按各自表的列语义提供
    （memories 与 records 的版本/质量语义不同，不可互相抄）。

    返回 {keep, drop, rule, basis, confident, note}。
    """
    def _vec(m, specs):
        return [f(m) for f, _ in specs]

    va, vb = _vec(ma, vers), _vec(mb, vers)
    qa, qb = _vec(ma, quals), _vec(mb, quals)
    if va != vb:
        keep_m, drop_m = (ma, mb) if va > vb else (mb, ma)
        rule = next((lab for (f, lab), x, y in zip(vers, va, vb) if x != y), "版本序")
        confident, note = True, ""
        # ③ 冲突检测：胜出者（新的那条）质量反而更弱 → 降级为「需人工看一眼」
        if _rank_of(keep_m, drop_m, quals) < 0:
            confident = False
            note = (f"#{drop_m.get('id')} 更旧但更可信（{_why(drop_m, keep_m, quals)}）；"
                    f"选 #{keep_m.get('id')} 的依据只有「更晚写入」——现实也许相反，请人工裁决")
        basis = "recency"
    else:
        # ② 版本信号打平 → 用质量信号 tiebreak
        cmp = _cmp(qa, qb)
        keep_m, drop_m = (ma, mb) if cmp >= 0 else (mb, ma)
        rule = (next((lab for (f, lab), x, y in zip(quals, qa, qb) if x != y), "")
                if cmp else "")
        if cmp:
            confident, note, basis = True, "", "tiebreak-quality"
        else:
            # 版本与质量信号都完全一致 → 用 id 做最终兜底：确定性仍需成立（不能靠遍历顺序）
            ia, ib = int(ma.get("id") or 0), int(mb.get("id") or 0)
            if ia == ib:
                return {"keep": ia, "drop": ib, "rule": "同 id（不应发生）",
                        "basis": "degenerate", "confident": False,
                        "note": "两条 id 相同，无法裁决"}
            keep_m, drop_m = (ma, mb) if ia > ib else (mb, ma)
            rule = "完全打平（回退 id 序）"
            confident, note, basis = False, "两条版本与质量信号完全一致——任选一条保留即可", "tiebreak-id"
    return {"keep": int(keep_m.get("id") or 0), "drop": int(drop_m.get("id") or 0),
            "rule": rule, "basis": basis, "confident": confident, "note": note}


# memories 的版本/质量信号定义（不可与 records 互抄——两表的列语义不同）：
#   版本：updated 为主（改写过 = 承载最新认知），created 兜底，id 最终 tiebreak
#   ISO 时间串的字典序 == 时间序，直接比字符串即可（避免解析开销与时区坑）
#   质量：confidence（0-1）→ 有无 evidence（有证据链更可信）→ maturity → use_count → pinned
# 注意：id **刻意不在** vers 里——它只是完全打平时的最终兜底。若把 id 塞进版本信号，
# 两条同时写入的记忆会因为 id 不同永远「不打平」，质量 tiebreak 就永远不会触发（2026-10-19
# 实测：正是这个 bug 让 _MEM_QUALS 全程失效）。
_MEM_VERS = [(lambda m: str(m.get("updated") or ""), "最后更新"),
             (lambda m: str(m.get("created") or ""), "创建时间")]
_MEM_QUALS = [(lambda m: float(m.get("confidence") or 0), "置信度"),
              (lambda m: 1 if (m.get("evidence") or "").strip() else 0, "证据链"),
              (lambda m: str(m.get("maturity") or ""), "成熟度"),
              (lambda m: int(m.get("use_count") or 0), "调用次数"),
              (lambda m: 1 if m.get("pinned") else 0, "置顶")]

# records 的版本/质量信号（语义独立于 memories）：
#   版本：created（入库时刻）→ date（记录标称日期，二者常不同：补记昨天的工作
#         时 date 是昨天、created 是今天）→ id。records 没有 updated 列。
#   质量：非 migrated 源（迁移拆分记录标题=小节名、语义完整性差）→ 非 auto-hook
#         （模板锚点几乎无信息量）→ 有 record_type（被分类过）→ 内容长度（长记录
#         通常信息更全）——长度是弱信号，放最后，仅作 tiebreak 用。
_REC_VERS = [(lambda r: str(r.get("created") or ""), "入库时间"),
             (lambda r: str(r.get("date") or ""), "记录日期")]
_REC_QUALS = [(lambda r: 0 if str(r.get("source") or "").startswith("migrated:") else 1, "非迁移来源"),
              (lambda r: 0 if "auto-hook" in str(r.get("title") or "") else 1, "非会话锚点"),
              (lambda r: 1 if str(r.get("record_type") or "").strip() else 0, "已分类"),
              (lambda r: int(r.get("n_chars") or 0), "内容长度")]


def _cmp(xs: list, ys: list) -> int:
    """逐元素字典序比较，返回 -1/0/1；类型不可比时退化为字符串比较（保证确定性、永不抛）。"""
    for x, y in zip(xs, ys):
        try:
            if x != y:
                return -1 if x < y else 1
        except TypeError:
            sx, sy = str(x), str(y)
            if sx != sy:
                return -1 if sx < sy else 1
    return 0


def _rank_of(a: dict, b: dict, quals: list) -> int:
    """a 相对 b 的质量序：-1=a 更弱，0=持平，1=a 更强。"""
    return _cmp([f(a) for f, _ in quals], [f(b) for f, _ in quals])


def _why(a: dict, b: dict, quals: list) -> str:
    """把「为什么 a 比 b 更可信」落成一句人话（标签来自调用方给的 quals）。"""
    for f, lab in quals:
        if f(a) != f(b):
            return f"{lab}更高"
    return "各项质量信号均更高"


_SIM_CACHE_MAX = 8
# root → (语料快照, threshold_used, cap_used, complete, frozen)
#   frozen: tuple of (a, b, sim_raw, verdict, content_a, content_b)，顺序即
#   `_similar_scan` 的返回顺序（未触 cap 时已按 -sim 排好）。
# R246：键从 R244 的「(root, threshold, cap) 精确匹配」放宽为「按语料快照 + 可派生」——
# 一次**未触 cap** 的扫描是所有更高阈值/更小上限请求的超集，阈值筛选与上限截断都只是
# 取子集，结果逐字等价；触过 cap 的扫描只能同参复用（截断发生在扫描序上，事后截断
# 不等价）。真实库一次巡检里 dup_mem（cap=10）与 ghost_memory（cap=50）都问
# threshold=0.55，此前各扫一遍 110ms + 74ms。
_SIM_CACHE: dict = {}

_REC_CACHE_MAX = 4
# (root, days, project) → (语料快照, threshold_used, cap_used, complete, frozen)
#   记录侧同款复用（R246）。快照含 title+content，565 行约 0.6MB；上限压到 4 份。
_REC_CACHE: dict = {}


def _similar_corpus(conn) -> list:
    """现行记忆的语料快照 [(id, kind, content)]，按 id 升序。

    R244：它就是「语料有没有变」的**定义本身**——缓存命中前逐行逐字比一遍，不靠
    COUNT/MAX(updated) 之类的间接签名去猜。真实库 388 行读一遍 ~2ms，比猜错便宜。
    为什么不用库文件 mtime：同库每次刷新都写 journal，mtime 必然变，缓存永不命中
    （R242 第一版实锤）。为什么不用 `updated` 时间戳：它是秒级，同一秒内的等长改写
    连 MAX(updated) 和 SUM(LENGTH(content)) 都不变——那种漏会让旧配对一直摆在屏幕上。
    """
    return [tuple(r) for r in conn.execute(
        "SELECT id, kind, content FROM memories WHERE " + _live_sql() + " ORDER BY id")]


def _similar_scan(root: str, corpus: list, threshold: float, cap: int) -> list:
    """无缓存的两两扫描本体（供 similar_memories 调用；单独拆出便于等值比对）。"""
    # 分词走统一缓存层的 sim 命名空间（与 similar_lessons_for 同一命名空间、同一值语义：
    # (root, memory_id) → _tokens(content)）。全库扫描一次就把每行的词集放进缓存，后续
    # 换 threshold/limit 重扫只需位集与对比。语料装不下缓存时不写（同 _bm25_rank 的理由）。
    use_cache = _TOK_CACHE_MAX <= 0 or len(corpus) <= _TOK_CACHE_MAX
    toks = []
    for mid, kind, content in corpus:
        t = _TOK_CACHE.get_sim(root, mid) if use_cache else None
        if t is None:
            t = _tokens(content)
            if use_cache:
                _TOK_CACHE.put_sim(root, mid, t)
        toks.append((mid, kind, content, t))
    bits, sizes = _bits_index([t[3] for t in toks])
    out = []
    for i in range(len(toks)):
        sa = sizes[i]
        if not sa:
            continue              # 空词集：与任何 j 的 sim 恒为 0，整行跳过（等价，省一层）
        ba = bits[i]
        for j in range(i + 1, len(toks)):
            sb = sizes[j]
            if not sb:
                continue
            # Jaccard 上界短路（B5）：|A∩B| <= min(|A|,|B|) 且 |A∪B| >= max(|A|,|B|)
            # → sim <= min/max。上界够不到阈值就不必做位集交——那是 O(N²) 里最贵的一步，
            # 也是 hub_metacognition（dup_pairs）在大库上的主开销。只跳「数学上不可能达阈值」
            # 的对，结果集合与迭代顺序完全不变（可回归比对）。
            lo, hi = (sa, sb) if sa < sb else (sb, sa)
            if lo / hi < threshold:
                continue
            # R235：位集交集的 popcount 与 len(set 交) 逐对严格相等（每 token 独占一位）
            inter = (ba & bits[j]).bit_count()
            # |A∪B| = |A|+|B|-|A∩B|：不必构造并集（省一次 O(|A|+|B|) 建集，纯等价代数替换）
            sim = inter / (sa + sb - inter)
            if sim >= threshold:
                out.append({"a": toks[i][0], "b": toks[j][0], "sim": round(sim, 2),
                            "_raw": sim,
                            "verdict": _verdict(toks[i][2], toks[j][2], toks[i][1] == toks[j][1]),
                            "content_a": toks[i][2][:80], "content_b": toks[j][2][:80]})
            if len(out) >= cap:
                return out
    out.sort(key=lambda x: -x["sim"])
    return out


def _sim_freeze(out: list) -> tuple:
    """扫描结果 → 不可变缓存条目；保留未取整的 sim 供跨阈值筛选。"""
    return tuple((p["a"], p["b"], p["_raw"], p["verdict"], p["content_a"], p["content_b"])
                 for p in out)


def _sim_thaw(frozen) -> list:
    """缓存条目 → 调用方拿到的 dict（每次新建，调用方可就地补字段而不脏缓存）。"""
    return [{"a": p[0], "b": p[1], "sim": round(p[2], 2), "verdict": p[3],
             "content_a": p[4], "content_b": p[5]} for p in frozen]


def _sim_cache_take(ent, corpus, threshold: float, cap: int):
    """从缓存条目派生本次请求的结果；不可安全派生时返回 None（由调用方真扫）。"""
    if ent is None or ent[0] != corpus:
        return None
    _corpus, thr0, cap0, complete, frozen = ent
    if threshold == thr0 and cap == cap0:
        return frozen                      # 同参：完整与否都逐字等价
    if not complete or thr0 > threshold:
        return None                        # 触过 cap，或缓存阈值更高 → 都不够用
    picked = [p for p in frozen if p[2] >= threshold]
    if len(picked) >= cap:
        return None                        # 本次会截断在扫描序上，事后截断不等价
    return tuple(picked[:cap])


def similar_memories(root: str, threshold: float = 0.55, limit: int = 10) -> list:
    """巩固：两两 Jaccard 相似度检测疑似重复/矛盾记忆（bigram 分词，<=500 条时全算）。
    返回 [{a, b, sim, verdict, content_a, content_b}]——verdict 为裁决辅助建议，最终由 agent/用户裁决。

    R244：结果按「语料快照」在进程内复用（分词另走 `_TOK_CACHE` 的 sim 命名空间）。
    真实库全量扫描 120ms（分词 36 + 位集 21 + 对比 63），而一次会话里同一份语料被
    五处各扫一遍：治理页 consolidate 段（10）、安全段 dup_mem 巡检（10）、统计页
    health_report（20）、metacognition dup_pairs（20）、ghost_memory（50）。
    R246：复用不再要求 (threshold, cap) 精确相同——一次未触上限的扫描即可服务更高
    阈值/更小上限的请求（见 `_sim_cache_take`），于是这五处只付一次全量扫描；
    真实库一次巡检里原本两次同语料扫描（110 + 74ms）现在第二次 ~5ms。
    返回的始终是新 dict：调用方（ghost_memory_candidates）会就地补 `suggestion`。
    """
    cap = max(1, min(int(limit), 50))
    threshold = float(threshold)
    with db_conn(root) as conn:
        corpus = _similar_corpus(conn)
        frozen = _sim_cache_take(_SIM_CACHE.get(root), corpus, threshold, cap)
        if frozen is not None:
            return _sim_thaw(frozen)
    out = _similar_scan(root, corpus, threshold, cap)
    frozen = _sim_freeze(out)
    _SIM_CACHE.pop(root, None)
    _SIM_CACHE[root] = (corpus, threshold, cap, len(out) < cap, frozen)
    while len(_SIM_CACHE) > _SIM_CACHE_MAX:
        _SIM_CACHE.pop(next(iter(_SIM_CACHE)))
    return _sim_thaw(frozen)


def ghost_memory_candidates(root: str, threshold: float = 0.55, limit: int = 10) -> list:
    """C4：潜在 **ghost memory**（幽灵记忆）检测——高相似但**没有取代关系**的记忆对。

    为什么需要它：业界给这个失效模式起了名字（llms3，2026-07）——生产中最常见的记忆故障
    **不是「忘了」，而是「记住太多版本」**：旧的/当前的/过渡态事实共存，被一起检索出来，
    模型自信地从混合物里综合出错误结论。A-TMA 给检索证据打 current/historical 状态标签后，
    冲突准确率 +0.240、temporal F1 0.030→0.171——说明「标出哪个版本是现行的」极有价值。

    AgentHub 本来就有解药：`supersedes` 取代链让旧版退出检索（`_live_sql()` 过滤）。
    **但前提是有人真的标了 supersedes**——没标的就会变成幽灵。本函数把「内容高度相似
    却又互无取代关系」的对挑出来（= 多个版本共存且没人裁决），**只报不裁**：
    最终由 agent/用户决定谁取代谁（与 similar_memories 的一贯立场一致）。

    D5 增量（2026-10-19）：每对额外带 `suggestion`——由**确定性代码**给出的
    「谁留谁退」建议（见 `_adjudicate`）。「只报不裁」的立场没变：建议存在与否
    都不改数据；它的价值是把 agent 从「自己想」压到「看一眼确认/改一下」。
    """
    pairs = similar_memories(root, threshold=threshold, limit=200)
    if not pairs:
        return []
    try:
        with db_conn(root) as conn:
            sup = {r["id"]: (r["superseded_by"] or 0)
                   for r in conn.execute("SELECT id, superseded_by FROM memories")}
            meta = {r["id"]: dict(r) for r in conn.execute(
                "SELECT id, created, updated, use_count, pinned, confidence, evidence, "
                "maturity FROM memories")}
    except Exception:  # noqa: BLE001
        return []
    out = []
    for p in pairs:
        a, b = p["a"], p["b"]
        if sup.get(a) == b or sup.get(b) == a:
            continue      # 已有取代关系 → 已治理，不是幽灵
        ma, mb = meta.get(a), meta.get(b)
        if ma and mb:  # 元数据缺一边（理论不可能）→ 宁可不给建议也不瞎给
            p["suggestion"] = _adjudicate(ma, mb, _MEM_VERS, _MEM_QUALS)
        out.append(p)
        if len(out) >= max(1, min(int(limit), 50)):
            break
    return out


def similar_records(root: str, threshold: float = 0.55, limit: int = 10,
                    days: int = 90, project: str = "") -> list:
    """记录版两两 Jaccard 相似度检测（与 similar_memories 同构；C4 遗留项，2026-10-18 补）。

    为什么单写一个而不是并入 similar_memories：records 与 memories 的**比较语义不同**——
    ① **同项目内比较**：记录带 project 维度，不同项目出现相同表述多是合理复用（模板头/
       通用结论），跨项目全比只制造噪音；要比单个项目传 project=，缺省按项目分组各自比；
    ② **只扫近 days 天**（默认 90）：records 体量远大于 memories（真实库 1398 vs 204），
       全量 O(N²) 不值当，且「同一内容被重复记录」本质是近期现象；
    ③ 只算 active 且未被取代的（_live_sql 同款过滤）——被 supersede 的旧版不该再参与
       「谁和谁像」的裁决；
    ④ 相似度用 **title + content** 拼接（记忆版只用 content）：记录标题是「目的」行的
       自动摘要，同一轮工作被记两遍时标题往往也同源，是有效信号。
    ⑤ **剥四段式模板头**（2026-10-18 实测后加）：【目的】【做了什么】【验证结果】【如何回滚】
       在每条 work_log 里都有，不剥掉等于给所有记录对发一份「基础相似度」——短记录光靠
       共享脚手架就能越过阈值。剥的是脚手架不是内容，与记忆版「只用 content」同精神。
    ⑥ **排除三类机器模板记录**（实测后加）：`migrated:*` 源（历史迁移按小节拆 工作记录.md，
       标题小节名，同标题对是语料 boilerplate）；标题含「（auto-hook）」的会话边界锚
       （Stop hook 每轮响应末记一条，同一 session 跨天anchors是设计使然、且整条都是
       模板+少量变量）；**source='auto' 的全部自动捕获记录**（2026-10-22 更正：此前把
       playbook 流程运行日志保留在候选里，理由是「同一流程重复记几条像真 ghost」；真实库
       实测 10 组候选 100% 来自 10-13 的 5 次 release 运行——备份文件名与 exe 时间各不相同，
       是事件流不是知识版本。自动记录由机器按节奏产生，其重复性归生产方的幂等性管；
       知识侧的 ghost 只该盯人工/agent 写的 hub 记录，否则每天的都是同一批正常日志）。
    返回 [{a, b, sim, project, verdict, agent_a, agent_b, date_a, date_b,
          title_a, title_b, content_a, content_b}]，按 sim 降序。

    R246：结果按「语料快照 + 可派生」在进程内复用（规则同 `similar_memories`）——
    同进程里反复巡检（GUI 再点一次自检、MCP 常驻进程多轮 hub_ops_run）时，第二次
    起只付读行 8ms + 快照比对，不再重付分词 115 + 位集 57 + 对比 76ms。
    ⑦ **record_type 参与 verdict**（R246 修）：原实现嘴上比 `record_type` 但 SELECT
    里没有这一列，`.get(..., "")` 两边同为 "" → 恒判「同类」，跨类型记录对被一律
    标成「疑似重复」。现在真读该列，跨类型对如实返回「相关」。
    """
    _AUTO_HOOK_MARK = "（auto-hook）"
    cap = max(1, min(int(limit), 50))
    threshold = float(threshold)

    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, project, agent, date, title, content, record_type FROM records WHERE "
            + _live_sql() + " AND date >= date('now', ?) AND source NOT LIKE 'migrated:%' "
            "AND source != 'auto' "
            "ORDER BY id", (f"-{int(days)} days",))]
    if project:
        rows = [r for r in rows if r["project"] == project]
    rows = [r for r in rows if _AUTO_HOOK_MARK not in (r["title"] or "")]

    snapshot = tuple((r["id"], r["project"], r["agent"], r["date"], r["title"],
                      r["content"], r["record_type"]) for r in rows)
    key = (root, int(days), project)
    frozen = _sim_cache_take(_REC_CACHE.get(key), snapshot, threshold, cap)
    if frozen is not None:
        return _rec_thaw(frozen)

    by_proj: dict = {}
    for r in rows:
        by_proj.setdefault(r["project"], []).append(r)
    groups = [[(r, _record_tokens(r["title"], r["content"])) for r in items]
              for items in by_proj.values()]
    out = _records_scan(groups, threshold, cap)
    frozen = _rec_freeze(out)
    _REC_CACHE.pop(key, None)
    _REC_CACHE[key] = (snapshot, threshold, cap, len(out) < cap, frozen)
    while len(_REC_CACHE) > _REC_CACHE_MAX:
        _REC_CACHE.pop(next(iter(_REC_CACHE)))
    return _rec_thaw(frozen)


def _record_tokens(title: str, content: str) -> set:
    """剥【…】段头后分词——记录的内容脚手架（四段式）不参与相似度，只比实质。"""
    return _tokens(re.sub(r"【[^】]*】", " ", f"{title} {content}"))


def _records_scan(groups: list, threshold: float, cap: int) -> list:
    """无缓存的两两扫描本体（groups = 按项目分好组的 [(row, token_set), ...]）。

    R246 从 `similar_records` 拆出：拆出来才能按语料快照整次复用，也便于等值比对。
    """
    out = []
    for toks in groups:
        bits, sizes = _bits_index([t[1] for t in toks])
        for i in range(len(toks)):
            ra = toks[i][0]
            sa = sizes[i]
            if not sa:
                continue
            ba = bits[i]
            for j in range(i + 1, len(toks)):
                rb = toks[j][0]
                sb = sizes[j]
                if not sb:
                    continue
                # Jaccard 上界短路（与 similar_memories 同款，B5）：数学上不可能达阈值
                # 就不做集合交——records 体量大，这一步是 O(N²) 里最贵的操作。
                lo, hi = (sa, sb) if sa < sb else (sb, sa)
                if lo / hi < threshold:
                    continue
                # R235：位集 popcount 与 len(set 交) 逐对严格相等（每 token 独占一位）
                inter = (ba & bits[j]).bit_count()
                sim = inter / (sa + sb - inter)   # |A∪B|=|A|+|B|-|A∩B|，免建并集
                if sim >= threshold:
                    out.append({"a": ra["id"], "b": rb["id"], "sim": round(sim, 2),
                                "_raw": sim,
                                "project": ra["project"],
                                "verdict": _verdict(f"{ra['title']} {ra['content']}",
                                                    f"{rb['title']} {rb['content']}",
                                                    ra["record_type"] == rb["record_type"]),
                                "agent_a": ra["agent"], "agent_b": rb["agent"],
                                "date_a": ra["date"], "date_b": rb["date"],
                                "title_a": ra["title"][:60], "title_b": rb["title"][:60],
                                "content_a": ra["content"][:80], "content_b": rb["content"][:80]})
                if len(out) >= cap:
                    out.sort(key=lambda x: -x["sim"])
                    return out
    out.sort(key=lambda x: -x["sim"])
    return out


def _rec_freeze(out: list) -> tuple:
    return tuple((p["a"], p["b"], p["_raw"], p["project"], p["verdict"], p["agent_a"],
                  p["agent_b"], p["date_a"], p["date_b"], p["title_a"], p["title_b"],
                  p["content_a"], p["content_b"]) for p in out)


def _rec_thaw(frozen) -> list:
    return [{"a": p[0], "b": p[1], "sim": round(p[2], 2), "project": p[3], "verdict": p[4],
             "agent_a": p[5], "agent_b": p[6], "date_a": p[7], "date_b": p[8],
             "title_a": p[9], "title_b": p[10], "content_a": p[11], "content_b": p[12]}
            for p in frozen]


def auto_record_count(root: str, days: int = 90) -> int:
    """近 days 天自动捕获记录数（source='auto'：会话锚 + playbook 流程运行日志）。

    幽灵检测已整体排除自动记录；这个计数只用于把「跳过了多少条机器事件流」如实报出来，
    让排除有据可查，而不是静默少报。"""
    try:
        with db_conn(root) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM records WHERE " + _live_sql()
                + " AND date >= date('now', ?) AND source='auto'",
                (f"-{int(days)} days",)).fetchone()
        return int(row[0] if row else 0)
    except Exception:  # noqa: BLE001
        return 0


def ghost_record_candidates(root: str, threshold: float = 0.55, limit: int = 10) -> list:
    """C4 遗留项（2026-10-18 补）：**记录版** ghost 检测——高相似却互无取代关系的记录对。

    ghost_memory_candidates 只查 memories，但 records 同样有 superseded_by 双时相列、
    同样会「记住太多版本」（同一轮工作被记两条、旧版没标取代关系——检索/时间线里两版
    并存，模型从混合物里综合）。口径与记忆版一致：近 90 天、同项目内比。只报不裁。
    """
    pairs = similar_records(root, threshold=threshold, limit=200)
    if not pairs:
        return []
    try:
        with db_conn(root) as conn:
            sup = {r["id"]: (r["superseded_by"] or 0)
                   for r in conn.execute("SELECT id, superseded_by FROM records")}
            meta = {r["id"]: dict(r) for r in conn.execute(
                "SELECT id, created, date, record_type, source, project, agent, "
                "length(content) AS n_chars FROM records")}
    except Exception:  # noqa: BLE001
        return []
    out = []
    for p in pairs:
        a, b = p["a"], p["b"]
        if sup.get(a) == b or sup.get(b) == a:
            continue      # 已有取代关系 → 已治理，不是幽灵
        ma, mb = meta.get(a), meta.get(b)
        if ma and mb:
            p["suggestion"] = _adjudicate(ma, mb, _REC_VERS, _REC_QUALS)
        out.append(p)
        if len(out) >= max(1, min(int(limit), 50)):
            break
    return out


# ---------------------------------------------------------------- D1/R2：写工具幂等键
# 为什么需要：agent 重试是必然事件（网络超时/循环重发/模型以为失败又发一次），
# 而不幂等的写工具会把重试变成重复记账——外部检索一致点名这是写工具第一优先项
# （CallSphere「最高优先级就是写工具幂等」；Thomas De Vos「超时=证据缺失≠失败」；
# datarekha「duplicate-call test 尤其有价值」）。AgentHub 的 log_work/memory_write
# 此前每次调用都产生新记录，超时重试即重复记录。
# 语义（默认完全兼容——键不传就走原路径）：
#   ① 同 (agent, key) 同内容 → 原样返回首次结果（重放），不重复执行；
#   ② 同 (agent, key) 不同内容 → 冲突拒收（digest 防键复用，见 Thomas De Vos）；
#   ③ key 按 agent 分命名空间（不同 agent 的同字符串 key 互不干扰）；
#   ④ 保留期 7 天（典型重试窗口），存储时顺带清理过期键。
IDEM_RETENTION_DAYS = 7
# 占位保护期：持有占位的进程崩溃/超时后，超过这个时长才允许他人接管同键。
# 写工具正常在秒级完成（最长含触发器预算 ~20s），120s 足够宽以免把慢请求判死。
IDEM_PENDING_GRACE_SEC = 120


def idem_digest(parts: dict) -> str:
    """内容指纹：同一 (agent, key) 携带不同内容 = 键复用冲突，拒收而非静默重放。"""
    import hashlib  # noqa: PLC0415
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def idem_begin(root: str, agent: str, key: str, digest: str, tool: str) -> tuple:
    """原子占位：检查与插入在同一写事务里完成，并发同键只有一个请求能执行。

    此前 lookup 与 store 分属两个事务：两个并发重试都能读到「未见」再各写一次，
    正是文件租约刚修过的同型竞态（R15）。返回 (状态, 载荷)：
    go=抢到占位可以执行 / replay=已有首次结果 / conflict=同键不同内容 /
    pending=另一请求正在执行 / busy=数据库写锁等待超时（可重试）。
    """
    now_dt = datetime.datetime.now()
    now = now_dt.isoformat(timespec="seconds")
    stale = (now_dt - datetime.timedelta(seconds=IDEM_PENDING_GRACE_SEC)).isoformat(
        timespec="seconds")
    with db_conn(root) as conn:
        busy = _begin_write(conn)
        if busy:
            return "busy", busy
        conn.execute("DELETE FROM idem_keys WHERE ts < datetime('now', ?)",
                     (f"-{IDEM_RETENTION_DAYS} days",))
        cur = conn.execute(
            "INSERT OR IGNORE INTO idem_keys(agent,key,tool,digest,result,ref,ts) "
            "VALUES(?,?,?,?,'',NULL,?)", (agent, key, tool, digest, now))
        if cur.rowcount == 1:
            return "go", ""
        row = conn.execute("SELECT digest,result,ts FROM idem_keys WHERE agent=? AND key=?",
                           (agent, key)).fetchone()
        if row is None:  # 插入与查询之间被清理：重占一次（写事务内实际不会发生）
            cur = conn.execute(
                "INSERT OR IGNORE INTO idem_keys(agent,key,tool,digest,result,ref,ts) "
                "VALUES(?,?,?,?,'',NULL,?)", (agent, key, tool, digest, now))
            return ("go", "") if cur.rowcount == 1 else ("pending", "")
        if row["digest"] != digest:
            return "conflict", ""
        if row["result"]:
            return "replay", row["result"]
        if row["ts"] < stale:
            # 占位超过保护期：视为持有者已崩溃，允许接管（删除+重插在同一写事务内）。
            conn.execute("DELETE FROM idem_keys WHERE agent=? AND key=? AND result='' AND ts<?",
                         (agent, key, stale))
            cur = conn.execute(
                "INSERT OR IGNORE INTO idem_keys(agent,key,tool,digest,result,ref,ts) "
                "VALUES(?,?,?,?,'',NULL,?)", (agent, key, tool, digest, now))
            if cur.rowcount == 1:
                return "go", ""
        return "pending", ""


def idem_lookup(root: str, agent: str, key: str, digest: str):
    """查幂等键。返回 None=未见/已过期（放行）/ str=首次结果（重放）/ 'conflict'=同键不同内容 /
    'pending'=另一请求已占位、尚未产出结果。"""
    stale = (datetime.datetime.now()
             - datetime.timedelta(seconds=IDEM_PENDING_GRACE_SEC)).isoformat(timespec="seconds")
    with db_conn(root) as conn:
        r = conn.execute("SELECT digest, result, ts FROM idem_keys "
                         "WHERE agent=? AND key=? AND ts >= datetime('now', ?)",
                         (agent, key, f"-{IDEM_RETENTION_DAYS} days")).fetchone()
    if not r:
        return None
    if r["digest"] != digest:
        return "conflict"
    if r["result"]:
        return r["result"]
    return "pending" if r["ts"] >= stale else None


def idem_store(root: str, agent: str, key: str, digest: str, tool: str,
               result: str, ref: int = 0) -> bool:
    """写成功后把占位行补成首次结果。返回是否补中自己的占位（False=占位已被接管/清理）。"""
    with db_conn(root) as conn:
        cur = conn.execute(
            "UPDATE idem_keys SET result=?, ref=?, ts=? "
            "WHERE agent=? AND key=? AND digest=? AND result=''",
            (result, ref or None, _now(), agent, key, digest))
        return cur.rowcount == 1


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
        # B10 收尾：写入热路径每次 log_work 都要扫全库 lesson/fact——不缓存就等于
        # 每写一条记录就把几百条记忆重新分词一遍。走统一缓存层的 sim 命名空间
        # （与 BM25 命名空间结构隔离——R6 踩坑：曾因键同型复用 BM25 缓存，set 存进
        # 存 Counter 的命名空间后 AttributeError；现由 _TokenCache typed 门面保证不可能）。
        if r["kind"] == "error":
            # errors 是**合成行**（title+detail 拼的、负 id、status 会变），不占缓存
            ot = _tokens(r["content"])
        else:
            ot = _TOK_CACHE.get_sim(root, r["id"])
            if ot is None:
                ot = _tokens(r["content"])
                _TOK_CACHE.put_sim(root, r["id"], ot)
        if not ot:
            continue
        # |A∪B| = |A|+|B|-|A∩B|：免建并集（R5 同款代数替换）
        sim = len(toks & ot) / (len(toks) + len(ot) - len(toks & ot))
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


def _idle_days(date_str: str, today: datetime.date = None) -> int | None:
    """YYYY-MM-DD[时间] → 闲置天数；无法解析返回 None（不猜）。"""
    try:
        d = datetime.date.fromisoformat((date_str or "").strip()[:10])
    except (ValueError, TypeError):
        return None
    return max(0, ((today or datetime.date.today()) - d).days)


def project_activity(root: str, stall_days: int = STALL_DAYS) -> dict:
    """项目活动快照（R20）：按最后一条工作记录/任务活动算真实闲置天数。

    为什么单列出来：`status='stalled'` 是**显式决策**（历史批量标记/人工判定），
    不等于「90 天无活动」——2026-10-22 实测 8 个停滞项目实际闲置只 6–47 天，
    旧检查却一律宣称「90 天无活动」，把人引向错误的归档理由。这里把两类分开：
    · labelled：状态已标停滞 → 决策队列（归档与否由用户拍板）；
    · drift：状态仍是 active 但真实闲置 ≥ stall_days → 漏标，才是维护故障。

    活动来源只认工作记录与任务：记忆可能是从旧项目蒸馏出的横向知识，写一条
    不代表该项目复工（真实案例：WiFi蓝牙项目 10-21 的记忆来自 ADB蓝牙调研），
    因此不用记忆刷新活跃，避免把已完结项目误判为在办。
    """
    today = datetime.date.today()
    with db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT name, status, updated FROM projects "
            "WHERE status IN ('active','stalled') ORDER BY name")]
        rec_last = {r["project"]: r["last"] for r in conn.execute(
            "SELECT project, MAX(date) AS last FROM records WHERE status='active' "
            "AND project!='' GROUP BY project")}
        task_last = {r["project"]: r["last"] for r in conn.execute(
            "SELECT project, MAX(substr(COALESCE(NULLIF(updated,''),created),1,10)) AS last "
            "FROM tasks WHERE project!='' GROUP BY project")}
    labelled, drift, no_activity = [], [], []
    for p in rows:
        cands = [(rec_last.get(p["name"], ""), "record"),
                 (task_last.get(p["name"], ""), "task")]
        cands = [(d, src) for d, src in cands if d]
        last, src = max(cands, key=lambda x: x[0]) if cands else ("", "")
        idle = _idle_days(last, today)
        item = {"name": p["name"], "status": p["status"], "updated": p["updated"],
                "last_activity": last[:10], "idle_days": idle, "source": src}
        if p["status"] == "stalled":
            labelled.append(item)
        elif idle is None:
            no_activity.append(item)
        elif idle >= stall_days:
            drift.append(item)
    labelled.sort(key=lambda x: (x["idle_days"] is None,
                                 -(x["idle_days"] or 0), x["name"]))
    drift.sort(key=lambda x: (-(x["idle_days"] or 0), x["name"]))
    return {"today": today.isoformat(), "stall_days": stall_days,
            "labelled": labelled, "drift": drift, "no_activity": no_activity}


def archive_project(root: str, name: str, agent: str = "") -> str:
    """项目归档（清理决策的落地动作）：状态改 archived（不再计入活跃/stalled）；
    目录存在则移入 <root>/99_Archive\\（可逆：restore_project 一步恢复）。
    add_record 对 archived 项目有 status!='archived' 守卫——归档后新记录不会误复活它。"""
    import shutil
    name = (name or "").strip()
    if not name:
        return "project 必填"
    perr = core.validate_project_ref(name)
    if perr:
        return perr
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


def restore_project(root: str, name: str, agent: str = "") -> str:
    """归档恢复（archive_project 的逆操作）：状态改回 active，目录从 99_Archive 移回根目录。
    先校验再动：根目录已有同名目录时拒绝，绝不覆盖未归档内容；归档目录缺失不阻塞状态恢复
    （归档时目录移动失败的项目也要能重新进入活跃视图）。updated 记为今天——恢复是「继续推进」
    的决定，不应在下一次 init 的 90 天自动标记里被立刻打回 stalled。"""
    name = (name or "").strip()
    if not name:
        return "project 必填"
    perr = core.validate_project_ref(name)
    if perr:
        return perr
    with db_conn(root) as conn:
        row = conn.execute("SELECT name, status FROM projects WHERE name=?", (name,)).fetchone()
        if not row:
            return f"未找到项目：{name}"
        if row["status"] != "archived":
            return f"项目未归档，无需恢复：{name}（当前状态 {row['status']}）"
    src = Path(root) / core.DIR_ARCHIVE / name
    dst = Path(root) / name
    if dst.exists():
        return f"未恢复：根目录已有同名 {name}\\（未改动状态与文件，请先处理冲突）"
    dir_found = src.is_dir()
    if dir_found:
        try:
            # 同卷原子改名：目标被并发创建时直接失败；shutil.move 会把源搬进目标目录里，
            # 造成「恢复成功但文件埋了一层」的假象
            os.rename(str(src), str(dst))
        except OSError as e:
            return f"未恢复：目录移回失败（状态仍为 archived）：{e}"
    try:
        with db_conn(root) as conn:
            conn.execute("UPDATE projects SET status='active', updated=? WHERE name=?",
                         (_now()[:10], name))
    except sqlite3.Error as e:
        if dir_found:
            try:
                os.rename(str(dst), str(src))
            except OSError:
                pass
        return f"未恢复：状态写回失败（目录已回滚）：{e}"
    if not dir_found:
        return f"状态已恢复为 active，但 99_Archive\\{name} 下无目录（归档时可能未移动）"
    return ""


def health_report(root: str, include_dups: bool = True,
                  stats_cache: dict | None = None) -> dict:
    """大脑体检：汇总各智能维度 + 数据规模，供 hub_health 工具与 GUI 体检摘要。

    include_dups=False：跳过重复/矛盾分析（`similar_memories`，真实库 125ms，
    占本报告 75%）。桌面「大脑页」从不渲染这两块，不该为自己的看不见付费。
    此时 dup_* / conflict_* 一律返回 None＝「没算过」——写成 0 组就是在撒谎，
    调用方要么用默认值，要么显式处理 None（`pages/brain_page.py` 两种都不用）。

    stats_cache：调用方已经算好 `stats(root)` 时传进来，省一次全量统计（20 万记录库
    实测 120ms，统计页本来就要一份 stats）。只读四个计数，传错结构会 KeyError。"""
    s = stats_cache if stats_cache is not None else stats(root)
    todos = extract_todos(root, limit=50)
    sims = similar_memories(root, limit=20) if include_dups else []
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
            "dup_memories": sims if include_dups else None,
            "dup_memory_count": len(sims) if include_dups else None,
            # 疑似互斥对（2026-10-13）：一条像在修正另一条——比"疑似重复"更危险，
            # 重复只是浪费上下文，互斥会让 agent 拿矛盾的事实干活。只报数不仲裁。
            "conflict_pairs": ([x for x in sims if "矛盾" in (x.get("verdict") or "")]
                               if include_dups else None),
            "conflict_pair_count": (sum(1 for x in sims if "矛盾" in (x.get("verdict") or ""))
                                    if include_dups else None),
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
            "mem_governance": (s["mem_governance"] if "mem_governance" in s
                               else memory_governance_stats(root)),
            # 0 条记录时记忆/记录无意义（max(1,0) 会报 200%）；返回 None 表示「样本不足」
            "crystallization": round(s["memories"] * 100 / s["records"], 1) if s["records"] else None}


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


def playbook_run_count(root: str, playbook: str = "") -> int:
    """执行历史总数（R276）：与 playbook_runs 同过滤口径，列表有 LIMIT。"""
    sql = "SELECT COUNT(*) FROM playbook_runs"
    args: list = []
    if playbook:
        sql += " WHERE playbook=?"
        args.append(playbook)
    try:
        with db_conn(root) as conn:
            return int(conn.execute(sql, args).fetchone()[0])
    except sqlite3.Error:
        return 0


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
                "SELECT id,title,project,due FROM tasks WHERE due!='' "
                "AND due<date('now','localtime') "
                "AND status NOT IN ('done','cancelled','dead') ORDER BY due ASC LIMIT 20")                 .fetchall()]
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