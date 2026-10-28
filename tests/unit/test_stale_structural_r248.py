# -*- coding: utf-8 -*-
"""stale_health 的泄漏判定：全量结构判定取代 5 条关键词抽样（R248）。

R248 实测（真实库，output/_r248_stale_cost.txt）：stale_health 热态 154.3ms 里
**142.5ms(92%)** 是 5 条抽样探针（单条 28.5 = BM25 全量拉行 11 + 图邻接全量构建 11 + LIKE 2.7），
新鲜度分布只 5.4ms。而四路候选全都只来自 `WHERE <_live_sql>`
（`_memories_query` / `_bm25_rank` / `_rows_by_ids` / `_semantic_rank` 的 JOIN），
融合之后只有折叠/重排/标注、不新增行 —— 于是「通过 live 谓词的已取代行 = 0」
**等价于**「四路都吐不出已取代行」，且覆盖全部行而不是 5 条抽样（R230 对语义路已用同一论证）。

本文件守四件事：
  ① 结构判定真的抓得住，且不漏出抽样上限（6 条已取代行全报，旧实现只取 5 条）；
  ② 金丝雀抓的是**接线**：谓词完好、管线被改成返回旧行时仍报泄漏（结构判定此时查不出，
     这条是唯一一道门）；
  ③ 每条通道单独都不返回已取代行（谓词若被某一路绕开，这里会红）；
  ④ 成本结构：一次检查最多一次检索；没有已取代行时零检索。
"""
import re

import brain
import ops


def _seeded(root, n_sup=1):
    """建 n_sup 条已取代记忆 + 1 条现行记忆，返回 (已取代 id 列表, 现行 id)。"""
    brain.init_db(root)
    live = brain.add_memory(root, "现行结论：部署目录在 D:/new/path", kind="fact", agent="t")
    sups = []
    for i in range(n_sup):
        old = brain.add_memory(root, f"旧结论{i}：端口配置在 808{i}", kind="fact", agent="t")
        brain.add_memory(root, f"新结论{i}：端口配置改为 909{i}", kind="fact", agent="t",
                         supersedes=old)
        sups.append(old)
    return sups, live


def test_structural_check_covers_every_superseded_row(hub_root, monkeypatch):
    """谓词被改坏时，6 条已取代行必须全部计入——旧实现只取 5 条，第 6 条静默漏掉。"""
    sups, _ = _seeded(hub_root, n_sup=6)
    monkeypatch.setattr(brain, "_live_sql", lambda a="": "1=1")
    r = ops.run_check("stale_health", "", hub_root)
    assert not r["ok"] and "泄漏" in r["summary"], r
    n = int(re.search(r"泄漏 (\d+) 条", r["summary"]).group(1))
    assert n >= len(sups), f"结构判定没覆盖全部已取代行：{r['summary']}"
    for mid in sups[:8]:
        assert any(f"#{mid}" in d for d in r["detail"]), (mid, r["detail"])


def test_canary_catches_leaky_pipeline_when_predicate_is_intact(hub_root, monkeypatch):
    """谓词完好（结构判定查不出），但管线被改成吐旧行 —— 只有金丝雀能抓到。"""
    sups, _ = _seeded(hub_root)
    old = sups[0]
    with brain.db_conn(hub_root) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE superseded_by != 0 AND "
                            + brain._live_sql()).fetchone()[0] == 0, "前提：谓词本身是好的"
    real = brain.search_memories

    def leaky(*a, **k):
        return real(*a, **k) + [{"id": old, "content": "旧结论0"}]

    monkeypatch.setattr(brain, "search_memories", leaky)
    r = ops._stale_health(hub_root)
    assert not r["ok"] and "泄漏" in r["summary"], r
    assert any(f"#{old}" in d for d in r["detail"]), r["detail"]


def test_each_channel_filters_superseded_rows(hub_root):
    """四路各自单独调用：已取代行一条都不许出现（图路用真实边连过去，不靠巧合）。"""
    sups, live = _seeded(hub_root)
    old = sups[0]
    brain.relation_add(hub_root, "memory", live, "memory", str(old), kind="link")
    kw = "旧结论0"
    assert all(r["id"] != old for r in brain._memories_query(hub_root, [kw], "", 100))
    assert all(r["id"] != old for r in brain._bm25_rank(hub_root, kw, "", 100))
    assert old not in brain._rows_by_ids(hub_root, "memory", [old])
    rows = brain.search_memories(hub_root, kw, "", 20, count_hits=False, semantic=False)
    assert all(r["id"] != old for r in rows), [r.get("id") for r in rows]


def test_one_search_at_most_and_zero_without_superseded(hub_root, monkeypatch):
    """成本结构：金丝雀最多跑一次检索；一条已取代行都没有时零检索。"""
    calls = []
    real = brain.search_memories
    monkeypatch.setattr(brain, "search_memories",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    brain.init_db(hub_root)
    brain.add_memory(hub_root, "只有现行记忆，没有旧版本", kind="fact", agent="t")
    r = ops._stale_health(hub_root)
    assert r["ok"] and not calls, f"没有已取代行却仍跑检索探针（{len(calls)} 次）"
    _seeded(hub_root, n_sup=7)
    calls.clear()
    ops._stale_health(hub_root)
    assert len(calls) == 1, f"一次检查应只跑 1 次检索（金丝雀），实际 {len(calls)}"
