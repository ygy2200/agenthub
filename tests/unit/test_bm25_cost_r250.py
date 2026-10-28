# -*- coding: utf-8 -*-
"""BM25 成本改造（R250）：df 只统计 query 词 + 窄列取行/top-k 回表。

R250 实测（真实库 output/_r250_bm25_cost.txt / _r250_ab.txt / _r250_prune.txt）：
  · 现实现 df.update(tf.keys()) 对每篇文档的**全部词表**做统计——记忆 5.5ms、记录 25ms，
    占 _bm25_score 的 99%；df 实际只在 w∈q_set 时被读，等价改写后 0.22/0.72ms。
  · SELECT * 拉全表后只输出 top-k 行：窄列取行 + top-k 回表，记忆再省 0.7ms、记录 2.9ms。
  · 交替 A/B 12 查询 × 2 表：分数 repr 与顺序 0 差异；全字段对拍 0 差异。
本文件守三件事：
  ① _bm25_score 与「全词表 df」旧实现逐分相同（含缺席词/并列分/空语料）；
  ② _bm25_rank / _records_bm25_rank 与「SELECT * 全量 + 旧 df」参照实现顺序、分数、字段全等；
  ③ 回表仍受存活谓词约束（已取代/过期行不得因回表复活），kind 过滤与 limit=0 边界不破。
"""
import math
from collections import Counter

import brain


def _ref_score(q_set, docs):
    """参照物：R250 之前的 _bm25_score（df 统计整篇词表）。逐字保留旧逻辑。"""
    n = len(docs)
    avgdl = sum(dl for _, _, dl in docs) / max(1, n)
    df = Counter()
    for _, tf, _dl in docs:
        df.update(tf.keys())
    scored = []
    for r, tf, dl in docs:
        s = 0.0
        for w in q_set:
            f = tf.get(w, 0)
            if not f:
                continue
            idf = math.log(1.0 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * f * (brain._BM25_K1 + 1.0) / (
                f + brain._BM25_K1 * (1.0 - brain._BM25_B + brain._BM25_B * dl / max(avgdl, 1e-9)))
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda t: (-t[0], t[1]["id"]))
    return scored


def _sig(rows, with_score=True):
    if with_score:
        return [(r["id"], repr(r["_bm25"])) for r in rows]
    return [{k: v for k, v in dict(r).items() if k != "_bm25"} for r in rows]


def _seed_memories(root):
    """混合语料：中文 bigram、英文标识符、并列分（同词同长）、tags/project 命中面。"""
    brain.init_db(root)
    data = [
        ("图扩展按层定向取边，检索热路径不再全表重建邻接表", "graph,性能", "AgentHub", "lesson"),
        ("图扩展：定向取边的等价性用旧源码对拍", "graph", "AgentHub", "lesson"),
        ("BM25 的 df 只在 query 词上统计就够了", "bm25,检索", "AgentHub", "lesson"),
        ("完全无关的一条记录", "misc", "", "note"),
        ("graph graph graph 多次出现的词", "", "", "note"),
    ]
    return [brain.add_memory(root, c, kind=k, tags=t, project=p) for c, t, p, k in data]


def _seed_records(root):
    brain.init_db(root)
    data = [
        ("AgentHub", "R250 BM25 成本改造", "df 只统计 query 词；窄列取行 + top-k 回表", "work_log"),
        ("AgentHub", "R249 图扩展", "按层定向取边，旧源码对拍 650 组合", "work_log"),
        ("其他", "无关记录", "和查询完全没有交集", "note"),
    ]
    return [brain.add_record(root, p, "qoder", "2026-10-28", t, c, record_type=rt)
            for p, t, c, rt in data]


def test_score_matches_full_vocabulary_df():
    """① 代表性语料上，与旧「全词表 df」实现逐分同序。"""
    docs = [
        ({"id": i}, Counter(toks), len(toks))
        for i, toks in enumerate([
            ["图", "扩展", "graph", "graph"],
            ["图", "扩展"],
            ["graph"],
            [],
            ["b", "m", "2", "5"],
        ], start=1)
    ]
    for q in (["图"], ["graph"], ["图", "扩展"], ["缺席词"], ["graph", "b"], []):
        assert brain._bm25_score(q, docs) == _ref_score(q, docs)


def test_score_edge_cases():
    """空语料不炸；q 命中但语料为空 → 空结果。"""
    assert brain._bm25_score(["图"], []) == []
    one = [({"id": 1}, Counter(["图"]), 1)]
    assert [s for s, _ in brain._bm25_score(["图"], one)] == [s for s, _ in _ref_score(["图"], one)]


def test_memory_rank_matches_legacy_full_scan(hub_root):
    """② 记忆通道：顺序、_bm25 与全字段（除 _bm25 外）与旧实现完全一致。"""
    ids = _seed_memories(hub_root)
    for q, lim in (("图扩展", 20), ("graph", 20), ("df query", 3), ("完全无关的查询词", 20)):
        got = brain._bm25_rank(hub_root, q, limit=lim)
        want = _legacy_memories_rank(hub_root, q, lim)
        assert _sig(got) == _sig(want), q
        assert _sig(got, with_score=False) == _sig(want, with_score=False), q
    assert len(brain._bm25_rank(hub_root, "图扩展", kind="lesson")) <= len(ids)


def test_records_rank_matches_legacy_full_scan(hub_root):
    """② records 通道端到端等价（标题/正文/项目三面）。"""
    _seed_records(hub_root)
    for q, lim in (("BM25", 30), ("定向取边", 30), ("qoder 无关", 2)):
        got = brain._records_bm25_rank(hub_root, q, limit=lim)
        want = _legacy_records_rank(hub_root, q, lim)
        assert _sig(got) == _sig(want), q
        assert _sig(got, with_score=False) == _sig(want, with_score=False), q


def test_rows_keep_all_columns(hub_root):
    """回表补齐全列：输出必须仍带 agent/created 等窄列查询没取的字段。"""
    _seed_memories(hub_root)
    _seed_records(hub_root)
    mem = brain._bm25_rank(hub_root, "图扩展", limit=5)
    rec = brain._records_bm25_rank(hub_root, "BM25", limit=5)
    assert mem and rec
    for key in ("agent", "created", "updated", "status", "superseded_by", "content"):
        assert key in mem[0]
    for key in ("agent", "date", "source", "status", "content"):
        assert key in rec[0]


def test_superseded_and_expired_rows_never_resurface(hub_root):
    """③ 回表仍受存活谓词约束：被取代/过期的行不能借回表复活。"""
    brain.init_db(hub_root)
    old = brain.add_memory(hub_root, "图扩展 旧版本内容", tags="graph")
    new = brain.add_memory(hub_root, "图扩展 新版本内容", tags="graph")
    brain.supersede_memory(hub_root, old, new)
    expired = brain.add_memory(hub_root, "图扩展 已过期内容", tags="graph",
                               valid_until="2000-01-01")
    got = brain._bm25_rank(hub_root, "图扩展", limit=20)
    assert new in [r["id"] for r in got]
    assert old not in [r["id"] for r in got]
    assert expired not in [r["id"] for r in got]


def test_limit_zero_returns_empty(hub_root):
    _seed_memories(hub_root)
    _seed_records(hub_root)
    assert brain._bm25_rank(hub_root, "图扩展", limit=0) == []
    assert brain._records_bm25_rank(hub_root, "BM25", limit=0) == []


def test_scores_rounded_to_four_decimals(hub_root):
    _seed_memories(hub_root)
    for r in brain._bm25_rank(hub_root, "图扩展", limit=20):
        assert r["_bm25"] == round(r["_bm25"], 4)


# ---- 参照实现：R250 之前的 _bm25_rank / _records_bm25_rank（SELECT * + 旧 df）


def _legacy_docs(root, kind):
    if kind == "mem":
        sql = "SELECT * FROM memories WHERE " + brain._live_sql()
        get = brain._TOK_CACHE.get_mem_bm25
        mk = lambda r: " ".join([r["content"] or "", r["tags"] or "", r["project"] or ""])  # noqa: E731
    else:
        sql = "SELECT * FROM records WHERE " + brain._rec_live_sql()
        get = brain._TOK_CACHE.get_records_bm25
        mk = lambda r: " ".join([r["title"] or "", r["content"] or "", r["project"] or ""])  # noqa: E731
    with brain.db_conn(root) as conn:
        rows = conn.execute(sql).fetchall()
    out = []
    for r in rows:
        ent = get(root, r["id"])
        if ent is None:
            toks = brain._tokenize(mk(r), max_tokens=2000)
            ent = (Counter(toks), len(toks))
        out.append((r, ent[0], ent[1]))
    return out


def _legacy_rank(root, query, limit, kind):
    q_set = list(dict.fromkeys(brain._tokenize(query)))
    if not q_set:
        return []
    triples = []
    for r, tf, dl in _legacy_docs(root, kind):
        triples.append((r, tf, dl))
    ranked = _ref_score(q_set, triples)[:max(0, min(int(limit), 100))]
    return [dict(r, _bm25=round(s, 4)) for s, r in ranked]


def _legacy_memories_rank(root, query, limit):
    return _legacy_rank(root, query, limit, "mem")


def _legacy_records_rank(root, query, limit):
    return _legacy_rank(root, query, limit, "rec")
