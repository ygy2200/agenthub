# -*- coding: utf-8 -*-
r"""R778：>20 万记录库的同步切页耗时（B024 续）。

R778 实测 20 万记录：today 同步 127ms、timeline 146ms（观察线 100ms），其余页 <35ms。
拆开同步路径：
- `team_digest(days=1)` 89.6ms：其中「近 N 天记录」两条查询把 date 包进
  `COALESCE(NULLIF(date,''), substr(created,1,10))`，索引无法用于范围过滤，实际扫全部
  active 行（47ms）+ GROUP BY 临时 B 树（44ms）；
- `records_agents` 87ms：`SELECT DISTINCT lower(agent)` 让 SQLite 建临时 B 树做 DISTINCT；
  改成取原始 agent 在 Python 里去重小写后 15.2ms；
- `records_page` 首页 64–80ms：`ORDER BY date DESC, id DESC` 无可用索引，临时排序 20 万行。

口径与 R769 相同：查询语义不变，只加覆盖索引 + 把 lower() 挪到 Python。本文件锁：
1. 两条新索引在 init_db 后真实存在；
2. 三处查询计划走新索引（防有人改回全表扫）；
3. `records_agents` 语义（小写去重排序）不变。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402


@pytest.fixture
def big_root(tmp_path):
    root = str(tmp_path)
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        conn.executemany(
            "INSERT INTO records(project,agent,date,title,content,source,created,"
            "valid_from,record_type) VALUES(?,?,?,?,?,?,?,?,?)",
            [(f"P{i % 6}", ("ZCode" if i % 3 else "qoder") if i % 5 else "",
              f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}", f"记录 {i}", "正文" * 5,
              "hub", "2026-10-11T00:00:00", "2026-10-11", "work_log")
             for i in range(3000)])
    return root


def test_indexes_exist_after_init(big_root):
    with brain.db_conn(big_root) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_records_status_day" in names
    assert "idx_records_status_date" in names


def _plan(conn, sql, args=()):
    return " ".join(str(r[3]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args))


def test_window_count_uses_day_index(big_root):
    """team_digest 的「近 N 天记录数」——表达式过滤必须能走表达式索引。"""
    sql = ("SELECT COUNT(*) FROM records WHERE status='active'"
           " AND COALESCE(NULLIF(date,''), substr(created,1,10))>=?")
    with brain.db_conn(big_root) as conn:
        plan = _plan(conn, sql, ("2026-10-29",))
    assert "idx_records_status_day" in plan, plan


def test_window_digest_uses_day_index(big_root):
    """同窗口的按 agent 聚合——R778 实测 20 万库 44ms（GROUP BY 前已扫全表）。"""
    sql = ("SELECT LOWER(TRIM(COALESCE(agent,''))) ag, COUNT(*) n,"
           " COUNT(DISTINCT COALESCE(NULLIF(date,''), substr(created,1,10))) days,"
           " MAX(COALESCE(NULLIF(date,''), substr(created,1,10))) last"
           " FROM records WHERE status='active'"
           " AND COALESCE(NULLIF(date,''), substr(created,1,10))>=? GROUP BY ag")
    with brain.db_conn(big_root) as conn:
        plan = _plan(conn, sql, ("2026-10-29",))
    assert "idx_records_status_day" in plan, plan


def test_first_page_order_uses_status_date_index(big_root):
    """时间线虚拟滚动首页：ORDER BY date DESC, id DESC 反向扫索引，不许临时排序。"""
    sql = ("SELECT * FROM records WHERE status='active'"
           " ORDER BY date DESC, id DESC LIMIT 50 OFFSET 0")
    with brain.db_conn(big_root) as conn:
        plan = _plan(conn, sql)
    assert "idx_records_status_date" in plan, plan
    assert "TEMP B-TREE" not in plan.upper(), plan


def test_records_agents_semantics_unchanged(big_root):
    """大小写混合 + 空 agent：小写去重、排序、空排除——与旧 DISTINCT lower(agent) 等价。"""
    agents = brain.records_agents(big_root)
    assert agents == ["qoder", "zcode"], agents
