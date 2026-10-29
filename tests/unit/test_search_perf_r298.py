# -*- coding: utf-8 -*-
r"""R298·hub_search 热路径三处省重复工作：结果必须逐位不变。

真实库（1597 记录 / 3445 记忆）实测一次 search_all 开 37 条 SQLite 连接、热态 283ms。
三处改动都只删「同一份数据的重复取用」，不碰召回/排序口径：
  ① `_cross_enrich` 逐候选单行取数 → 每方向一次 IN 查询（16/37 条连接来自这里）；
  ② `_keep_wal` 每条连接都 stat 一次库文件（Windows 实测 1.4ms/次）→ 哨兵命中就不 stat；
  ③ `_coverage_gate` 的 df 在 records/memories 两次调用间重复整表扫 → 单次 search_all 内复用。
这里钉住三件事：批量后**顺序与去重不变**、memo **只在 scope 内**生效且返回副本、
哨兵命中时**不再碰文件系统**。
"""
import os

import brain
import pytest


def _seed_rows(root):
    brain.init_db(root)
    brain.add_record(root, "甲-项目", "t", "2026-01-01", "sqlite 并发锁",
                     "sqlite 并发 锁 只读 数据库 复现")
    brain.add_memory(root, "sqlite 并发锁 的修法是 WAL 常驻连接", kind="lesson", agent="t")


# ── ① 跨类型图增强：批量取数，顺序/去重/标记不变 ─────────────────────────────

def test_cross_enrich_batches_one_query_per_direction(monkeypatch):
    calls = []

    def fake_expand(root, src, dst, seeds, limit=8):
        # 1 已在目标列表里（必须被跳过、不进 IN 查询）
        return [(1, 9.0), (2, 5.0), (3, 1.0)]

    def fake_rows(root, entity, ids):
        calls.append((entity, list(ids)))
        return {i: {"id": i, "content": f"{entity}-{i}"} for i in ids}

    monkeypatch.setattr(brain, "_graph_expand_cross", fake_expand)
    monkeypatch.setattr(brain, "_rows_by_ids", fake_rows)
    out = {"records": [{"id": 1, "_channels": {"like": 1}}],
           "memories": [{"id": 1, "_channels": {"bm25": 2}}]}
    brain._cross_enrich("ignored", out)

    # 每方向恰好一次批量查询；已在目标列表里的 id 不再请求
    assert calls == [("memory", [2, 3]), ("record", [2, 3])], calls
    assert [x["id"] for x in out["memories"]] == [1, 2, 3]
    assert [x["id"] for x in out["records"]] == [1, 2, 3]
    assert all(x.get("_cross") == "来自record" for x in out["memories"][1:])
    assert all(x.get("_cross") == "来自memory" for x in out["records"][1:])


def test_cross_enrich_no_query_when_all_ids_known(monkeypatch):
    calls = []
    monkeypatch.setattr(brain, "_graph_expand_cross", lambda *a, **k: [(1, 9.0)])
    monkeypatch.setattr(brain, "_rows_by_ids",
                        lambda root, entity, ids: calls.append(entity) or {})
    # 两个方向的目标列表都已有 id=1 —— 全部命中已存在分支
    out = {"records": [{"id": 1, "_channels": {"like": 1}}],
           "memories": [{"id": 1, "_channels": {"like": 1}}]}
    brain._cross_enrich("ignored", out)
    assert calls == []


# ── ② 哨兵命中时不再 stat 库文件 ─────────────────────────────────────────────

def test_keep_wal_skips_exists_when_sentinel_alive(hub_root, monkeypatch):
    _seed_rows(hub_root)
    brain._keep_wal(hub_root)                      # 建立常驻哨兵
    real_exists = os.path.exists

    def boom(p):
        if str(p).endswith("brain.db"):
            raise AssertionError("哨兵命中时不该再 stat 库文件")
        return real_exists(p)

    monkeypatch.setattr(brain.os.path, "exists", boom)
    try:
        brain._keep_wal(hub_root)                  # 不应抛
        with brain.db_conn(hub_root) as conn:      # 正常连接也不应 stat
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        monkeypatch.setattr(brain.os.path, "exists", real_exists)
        brain.release_root(hub_root)


def test_keep_wal_still_creates_sentinel_after_release(hub_root):
    _seed_rows(hub_root)
    brain._keep_wal(hub_root)
    assert len(brain._SENTINELS) >= 1
    brain.release_root(hub_root)
    brain._keep_wal(hub_root)                      # 释放后重走 stat + 新开，不应崩
    assert str(brain.db_path(hub_root)) in brain._SENTINELS
    brain.release_root(hub_root)


# ── ③ df memo：仅 scope 内生效、返回副本、key 含 root ────────────────────────

def _count_conns(monkeypatch):
    n = {"c": 0}
    orig = brain.db_conn

    def counted(root):
        n["c"] += 1
        return orig(root)

    monkeypatch.setattr(brain, "db_conn", counted)
    return n


def test_df_memo_reuses_within_scope_only(hub_root, monkeypatch):
    _seed_rows(hub_root)
    tokens = ["sqlite", "并发", "锁"]
    n = _count_conns(monkeypatch)

    with brain._df_memo_scope():
        df1, total1 = brain._token_stats(hub_root, tokens)
        df2, total2 = brain._token_stats(hub_root, tokens)
    inside = n["c"]
    assert df1 == df2 and total1 == total2
    assert inside == 1, f"scope 内应只扫一次，实际 {inside} 次"

    brain._token_stats(hub_root, tokens)           # scope 外：不得复用
    assert n["c"] == inside + 1


def test_df_memo_returns_copy_not_shared_state(hub_root, monkeypatch):
    _seed_rows(hub_root)
    tokens = ["sqlite", "并发"]
    with brain._df_memo_scope():
        df1, _ = brain._token_stats(hub_root, tokens)
        df1["sqlite"] = 999                        # 调用方改返回值
        df2, _ = brain._token_stats(hub_root, tokens)
    assert df2["sqlite"] != 999 and df2["sqlite"] >= 1


def test_df_memo_keyed_by_root(hub_root, tmp_path, monkeypatch):
    _seed_rows(hub_root)
    other = tmp_path / "other-root"
    other.mkdir()
    brain.init_db(str(other))
    brain.add_record(str(other), "乙-项目", "t", "2026-01-01", "空库",
                     "此处没有 sqlite 并发锁 相关内容")
    n = _count_conns(monkeypatch)
    with brain._df_memo_scope():
        df_a, _ = brain._token_stats(hub_root, ["sqlite"])
        df_b, _ = brain._token_stats(str(other), ["sqlite"])
    assert n["c"] == 2, "不同 root 不得共用 df 缓存"
    assert df_a != df_b


def test_search_all_results_unchanged_by_memo(hub_root):
    _seed_rows(hub_root)
    a = brain.search_all(hub_root, "sqlite 并发锁 只读")
    b = brain.search_all(hub_root, "sqlite 并发锁 只读")
    key = lambda out: {k: [(x.get("id"), x.get("_channels"), x.get("_cross"), x.get("_weak"))
                           for x in v] for k, v in out.items()}   # noqa: E731
    assert key(a) == key(b)
    assert a["records"] and a["memories"]
