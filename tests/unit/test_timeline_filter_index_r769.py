# -*- coding: utf-8 -*-
r"""R769：时间线切页同步阻塞——B021。

R768 在 6 万记录库实测：切到时间线页同步阻塞 212/242ms，根因分解为
`list_projects(200)` 123.9ms + `records_agents` 35.6ms（`records_page` 首页仅 12.2ms），
即慢在**同步填两个过滤器下拉**：`list_projects` 对每个项目做 MAX(date)/COUNT(*)
相关子查询，逐条回表扫 records。

口径：不改查询语义，只加覆盖索引（`ensure_schema` 每次 executescript(SCHEMA)，
旧库自动补建）。本文件锁三件事：
1. init_db 后两条索引真实存在（旧库也能补上）；
2. list_projects 的查询计划走覆盖索引（不再回表）；
3. 大数据量下两条下拉查询的耗时上限（宽阈值，防退化成全表回表；不追求精确数字）。
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402


@pytest.fixture
def big_root(tmp_path):
    """3000 条记录 / 6 个项目：够让回表代价显形，又不拖慢单测。"""
    root = str(tmp_path)
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        conn.executemany(
            "INSERT INTO records(project,agent,date,title,content,source,created,"
            "valid_from,record_type) VALUES(?,?,?,?,?,?,?,?,?)",
            [(f"P{i % 6}", "zcode" if i % 2 else "qoder",
              f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}", f"记录 {i}", "正文" * 5,
              "hub", "2026-10-11T00:00:00", "2026-10-11", "work_log")
             for i in range(3000)])
        conn.executemany("INSERT OR IGNORE INTO projects(name,created) VALUES(?,?)",
                         [(f"P{k}", "2026-10-11") for k in range(6)])
    return root


def test_indexes_exist_after_init(big_root):
    with brain.db_conn(big_root) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_records_proj_status_date" in names
    assert "idx_records_status_agent" in names


def test_list_projects_uses_covering_index(big_root):
    sql = (
        "SELECT p.name, p.agent, p.created, p.status,"
        " (SELECT MAX(date) FROM records r WHERE r.project=p.name AND r.status='active') AS last_active,"
        " (SELECT COUNT(*) FROM records r WHERE r.project=p.name AND r.status='active') AS n_records"
        " FROM projects p ORDER BY last_active DESC, p.name LIMIT 200")
    with brain.db_conn(big_root) as conn:
        plan = " ".join(str(r[3]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
    assert "COVERING INDEX idx_records_proj_status_date" in plan, plan


def test_filter_queries_stay_fast_on_big_db(big_root):
    """宽阈值 150ms（R768 基线：6 万记录时两条合计 ~160ms）。回表退化会先在这里冒红。"""
    t0 = time.perf_counter()
    projs = brain.list_projects(big_root, 200)
    agents = brain.records_agents(big_root)
    dt_ms = (time.perf_counter() - t0) * 1000
    assert {p["name"] for p in projs} == {f"P{i}" for i in range(6)}
    last = [p["last_active"] or "" for p in projs]
    assert last == sorted(last, reverse=True), f"未按最近活动倒序：{last}"
    assert sorted(agents) == ["qoder", "zcode"]
    assert dt_ms < 150, f"两条下拉查询合计 {dt_ms:.1f}ms（阈值 150ms）"
