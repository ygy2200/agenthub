# -*- coding: utf-8 -*-
"""图扩展取边：按层定向取边取代全表邻接（R249）。

R249 实测（真实库 output/_r249_bench.txt）：每次检索都全表重建邻接表 7.7-7.9ms，
而遍历真正读到的只有「种子 + 1..hops-1 层」的边，定向取 1.9-2.7ms；检索总数
52.4→46.1ms、_cross_enrich 9.4→1.6ms、stale_health 20.9→17.4ms。

等价性在真实库上以 HEAD 旧源码对拍 650 组 0 差异（output/_r249_verify.py）。
本文件守四件事：
  ① 混合图（多跳/重复边/自环/非数字 ref/tag/project 枢纽）上与全表邻接**逐位同分**；
  ② 最后一层按对端类型过滤不会漏掉「只能经枢纽到达」的目标节点；
  ③ 强制走分块（_GRAPH_SQL_CHUNK=1）仍与全表一致——IN 列表分段不能改变结果；
  ④ 回归守卫：_graph_expand/_graph_expand_cross 不再读全表（_graph_adj 被禁仍工作）。
"""
import sqlite3

import brain


def _seed_graph(root):
    """混合图：memory 1/2/3、record 10/11、tag alpha、project P（tag/project 只在 to 侧）。

    1 -mention-> 2、1 -link-> 2（同对重复边，不同边型）
    1 -tag-> alpha、3 -tag-> alpha（两跳：1→alpha→3）
    2 -in-> P（记忆挂项目）、10 -in-> P、10 -mention-> 11
    3 -mention-> 3（自环）
    """
    brain.init_db(root)
    edges = [
        ("memory", 1, "memory", "2", "mention"),
        ("memory", 1, "memory", "2", "link"),
        ("memory", 1, "tag", "alpha", "tag"),
        ("memory", 3, "tag", "alpha", "tag"),
        ("memory", 2, "project", "P", "in"),
        ("record", 10, "project", "P", "in"),
        ("record", 10, "record", "11", "mention"),
        ("memory", 3, "memory", "3", "mention"),
    ]
    with brain.db_conn(root) as conn:
        for e in edges:
            conn.execute("INSERT INTO relations(from_kind,from_id,to_kind,to_ref,kind,created)"
                         " VALUES(?,?,?,?,?,'2026-10-28T00:00:00')", e)


def _full_adj(conn):
    """参照物：生产里仍在的 _graph_adj（全表邻接，graph_clusters 用）。"""
    return brain._graph_adj(conn)


def _ref_expand(root, entity, seeds, limit=30):
    """参照物：R249 之前的实现（全表邻接 + 同样 BFS）。逐字保留旧逻辑。"""
    if not seeds or brain._RRF_W_GRAPH <= 0:
        return []
    try:
        with brain.db_conn(root) as conn:
            adj = _full_adj(conn)
    except sqlite3.Error:
        return []
    scores = {}
    for seed in {(entity, str(s)) for s in seeds}:
        seen = {seed}
        frontier = [seed]
        for depth in range(1, brain._GRAPH_HOPS + 1):
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


def _ref_cross(root, src, dst, seeds, limit=10):
    if not seeds:
        return []
    try:
        with brain.db_conn(root) as conn:
            adj = _full_adj(conn)
    except sqlite3.Error:
        return []
    scores = {}
    for seed in {(src, str(s)) for s in seeds}:
        seen = {seed}
        frontier = [seed]
        for depth in range(1, brain._GRAPH_HOPS + 1):
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
                        0.5 if ek in ("mention", "link") else 0)) * 0.5
            frontier = nxt
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:max(1, min(limit, 20))]


def test_matches_full_table_adjacency_on_mixed_graph(hub_root):
    """① 多跳/重复边/自环/tag 与 project 枢纽：分数与顺序逐位相同。"""
    assert brain._RRF_W_GRAPH > 0, "用例前提：图路权重开启"
    _seed_graph(hub_root)
    cases = [
        ("memory", [1]), ("memory", [2]), ("memory", [3]), ("memory", [1, 2]),
        ("memory", [1, 2, 3]), ("memory", [11]), ("record", [10]), ("record", [10, 11]),
        ("memory", []), ("memory", [999]),
    ]
    for entity, seeds in cases:
        for limit in (5, 30, 50):
            assert brain._graph_expand(hub_root, entity, seeds, limit=limit) == \
                _ref_expand(hub_root, entity, seeds, limit=limit), (entity, seeds, limit)


def test_scores_pin_semantics(hub_root):
    """①+② 不只是「跟旧的一样」，把计分口径也钉住：重复边只算首次、两跳 1.0/跳。"""
    _seed_graph(hub_root)
    got = brain._graph_expand(hub_root, "memory", [1])
    # 2 = 一跳 mention(2.0+0.5)，同对的 link 边因「已见」不再计分；3 = 经 tag 两跳(2.0/2)
    assert got == [(2, 2.5), (3, 1.0)], got


def test_hub_with_many_other_kinds_still_reaches_target(hub_root):
    """② 最后一层按目标类型过滤：枢纽上挂一堆非目标边，目标仍须经它被捞到。"""
    brain.init_db(hub_root)
    with brain.db_conn(hub_root) as conn:
        rows = [("memory", 7, "tag", "hub", "tag"), ("memory", 8, "tag", "hub", "tag"),
                ("memory", 100, "tag", "hub", "tag"), ("memory", 101, "tag", "hub", "tag")]
        rows += [("record", 200 + i, "tag", "hub", "tag") for i in range(20)]
        for e in rows:
            conn.execute("INSERT INTO relations(from_kind,from_id,to_kind,to_ref,kind,created)"
                         " VALUES(?,?,?,?,?,'2026-10-28T00:00:00')", e)
    got = brain._graph_expand(hub_root, "memory", [7])
    assert got == [(8, 1.0), (100, 1.0), (101, 1.0)], got
    assert brain._graph_expand(hub_root, "memory", [7]) == _ref_expand(hub_root, "memory", [7])


def test_string_seed_refs_match_int_refs(hub_root):
    """种子 id 传 '1' 与 1 结果相同（调用方可能从 JSON 拿到字符串 id）。"""
    _seed_graph(hub_root)
    assert brain._graph_expand(hub_root, "memory", ["1"], limit=50) == \
        brain._graph_expand(hub_root, "memory", [1], limit=50) != []


def test_chunked_in_lists_match(hub_root, monkeypatch):
    """③ 分段 IN 列表（每段 1 个 id）不改变结果——种子一多就会真的分段。"""
    _seed_graph(hub_root)
    monkeypatch.setattr(brain, "_GRAPH_SQL_CHUNK", 1)
    for entity, seeds in (("memory", [1, 2, 3]), ("record", [10, 11])):
        assert brain._graph_expand(hub_root, entity, seeds, limit=50) == \
            _ref_expand(hub_root, entity, seeds, limit=50)
    assert brain._graph_expand_cross(hub_root, "record", "memory", [10, 11]) == \
        _ref_cross(hub_root, "record", "memory", [10, 11])


def test_cross_expand_matches_reference(hub_root):
    _seed_graph(hub_root)
    for src, dst, seeds in (("memory", "record", [1, 2, 3]), ("record", "memory", [10, 11]),
                            ("memory", "record", [10]), ("record", "memory", [])):
        assert brain._graph_expand_cross(hub_root, src, dst, seeds) == \
            _ref_cross(hub_root, src, dst, seeds), (src, dst, seeds)


def test_expand_never_loads_full_adjacency(hub_root, monkeypatch):
    """④ 回归守卫：想改回全表邻接（_graph_adj）会立刻红——这是 R249 的收益来源。"""
    _seed_graph(hub_root)
    want = brain._graph_expand(hub_root, "memory", [1], limit=50)

    def boom(conn):
        raise AssertionError("_graph_expand 不该再全表加载邻接表（R249）")

    monkeypatch.setattr(brain, "_graph_adj", boom)
    assert brain._graph_expand(hub_root, "memory", [1], limit=50) == want
    assert brain._graph_expand_cross(hub_root, "memory", "record", [1]) == []


def test_empty_relations_and_degenerate_seeds(hub_root):
    brain.init_db(hub_root)
    assert brain._graph_expand(hub_root, "memory", [1]) == []
    assert brain._graph_expand(hub_root, "memory", []) == []
    assert brain._graph_expand_cross(hub_root, "memory", "record", [1]) == []
    with brain.db_conn(hub_root) as conn:
        conn.execute("INSERT INTO relations(from_kind,from_id,to_kind,to_ref,kind,created)"
                     " VALUES('memory',1,'tag','t','tag','2026-10-28T00:00:00')")
    assert brain._graph_expand(hub_root, "memory", [1]) == []
    assert brain._graph_expand(hub_root, "memory", ["1"]) == []
