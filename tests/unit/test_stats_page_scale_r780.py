# -*- coding: utf-8 -*-
r"""R780：统计页在 >20 万记录库要等 664ms（骨架屏遮着，但数据到得慢）——B024 后续。

R778 复测 20 万库时统计页总耗时 664ms（大脑页 516ms），逐项拆：
- `health_report` 432ms：其中 `bare_titles` 的 `title GLOB 'YYYY-MM-DD（*）'`（字符类开头，
  索引无法用于前缀匹配）扫全部 active 行 **205ms**；且它内部又调了一次 `stats()`（120ms），
  而调用方（统计页）自己已经算过一份；
- `extract_todos` 63ms：`WHERE status='active' ORDER BY id DESC LIMIT 400` 没有可用的
  `(status, id)` 索引，SQLite 要么给 20 万行排序、要么扫完再停。

本文件锁三件事：
1. 两条索引存在且能被对应查询用上（EXPLAIN QUERY PLAN）；
2. `extract_todos` 的查询不再出现 TEMP B-TREE；
3. `health_report(stats_cache=…)` 复用调用方算好的 stats，不再重复查三张表。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402


@pytest.fixture
def seeded_root(tmp_path):
    root = str(tmp_path)
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        conn.executemany(
            "INSERT INTO records(project,agent,date,title,content,source,created,"
            "valid_from,record_type) VALUES(?,?,?,?,?,?,?,?,?)",
            [(f"P{i % 5}", "zcode", "2026-10-11",
              f"2026-10-11（工作记录 {i}）" if i % 3 == 0 else f"正常标题 {i}",
              "目的：… 验证：…", "hub", "2026-10-11T00:00:00", "2026-10-11", "work_log")
             for i in range(1200)])
    return root


def _plan(conn, sql, args=()):
    return " ".join(str(r[3]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args))


def test_new_indexes_exist(seeded_root):
    with brain.db_conn(seeded_root) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_records_status_id" in names
    assert "idx_records_bare_title" in names


def test_todos_query_uses_status_id_index(seeded_root):
    sql = ("SELECT id, project, date, agent, content FROM records"
           " WHERE status='active' ORDER BY id DESC LIMIT 400")
    with brain.db_conn(seeded_root) as conn:
        plan = _plan(conn, sql)
    assert "idx_records_status_id" in plan, plan
    assert "TEMP B-TREE" not in plan.upper(), plan


def test_bare_title_count_uses_partial_index(seeded_root):
    sql = ("SELECT COUNT(*) FROM records WHERE status='active'"
           " AND title GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]（*）'")
    with brain.db_conn(seeded_root) as conn:
        plan = _plan(conn, sql)
    assert "idx_records_bare_title" in plan, plan


def test_bare_title_count_value_unchanged(seeded_root):
    h = brain.health_report(seeded_root, include_dups=False)
    # 1200 条里 i%3==0 共 400 条是「日期（…）」裸标题
    assert h["bare_titles"] == 400, h["bare_titles"]


def test_health_report_reuses_stats_cache(seeded_root, monkeypatch):
    calls = []
    real = brain.stats

    def spy(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(brain, "stats", spy)
    cached = brain.stats(seeded_root)
    assert len(calls) == 1
    h = brain.health_report(seeded_root, include_dups=False, stats_cache=cached)
    assert len(calls) == 1, "health_report 拿到 stats_cache 还又算了一遍 stats"
    assert h["records"] == cached["records"] and h["projects"] == cached["projects"]
    # 不给缓存时行为不变
    brain.health_report(seeded_root, include_dups=False)
    assert len(calls) == 2
