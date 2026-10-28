# -*- coding: utf-8 -*-
r"""R245·陈旧体检的泄漏探针跳过覆盖度闸门：闸门只删不添，跳过它只会更严。

背景（真实库实测，output/_r245_stale_cost.txt）：`stale_health` 287.8ms 里 97%
（282.8ms）是 5 次泄漏探针检索；单次检索 69.6ms，其中覆盖度闸门 40.8ms（全是
`_token_stats` 的 LIKE 全表扫，随 token 数线性涨）、融合检索 28.7ms。
探针的语义是「**这条已取代的记忆还能不能被检索管线吐出来**」——要的是**超集**：
闸门（`_coverage_gate`）只把候选分成 strong/weak 再**删**，删掉的越多漏报越多；
跳过它只会把更宽的候选集喂给探针，检测严格更强，不会变弱。
融合层不能跳：`_apply_graph_channel` 是**会加行**的第四通道，跳了可能漏真泄漏。

本文件钉住：① gate=False 的结果是 gate=True 的超集；② 结构过滤（_live_sql）
不归闸门管，已取代行在 gate=False 下仍不出现；③ 探针确实传 gate=False 且条数
仍是 min(5, 已取代数)；④ gate=False 不再碰 `_token_stats`；⑤ 真泄漏仍报红。
"""
import brain
import ops


def _seed(root):
    brain.init_db(root)
    a = brain.add_memory(root, "紫色独角兽在冰川上跳舞", kind="fact", agent="t")
    b = brain.add_memory(root, "紫色和冰川只是普通词，跳舞也是另一回事", kind="fact", agent="t")
    return a, b


def _supersede_pair(root, i):
    old = brain.add_memory(root, f"旧事实第{i}号：发布节奏按里程碑走，每两周一次", kind="fact", agent="t")
    new = brain.add_memory(root, f"新事实第{i}号：发布节奏改为每月一次，按整月切", kind="fact", agent="t")
    brain.supersede_memory(root, old, new)
    return old, new


def test_gate_off_returns_superset(hub_root):
    """闸门会删（只留 strong），跳过闸门必须把 weak 也带上——探针要的是超集。"""
    a, b = _seed(hub_root)
    q = "紫色独角兽在冰川上跳舞"
    on = brain.search_memories(hub_root, q, "", 10, count_hits=False, semantic=False)
    off = brain.search_memories(hub_root, q, "", 10, count_hits=False, semantic=False, gate=False)
    on_ids = {r["id"] for r in on}
    off_ids = {r["id"] for r in off}
    assert a in on_ids, f"强匹配行不该被闸门删：{sorted(on_ids)}"
    assert on_ids == {a}, f"弱匹配行应被闸门过滤掉，实得 {sorted(on_ids)}"
    assert {a, b} <= off_ids, f"跳过闸门的探针结果应是全量候选：{sorted(off_ids)}"
    assert on_ids <= off_ids


def test_gate_off_still_hides_superseded(hub_root):
    """已取代行的排除靠 _live_sql 的结构过滤，与闸门无关——gate=False 不许放行。"""
    brain.init_db(hub_root)
    old, new = _supersede_pair(hub_root, 1)
    rows = brain.search_memories(hub_root, "旧事实第1号：发布节奏按里程碑走", "", 10,
                                 count_hits=False, semantic=False, gate=False)
    assert old not in {r["id"] for r in rows}, rows


def test_stale_health_probes_pass_gate_off(hub_root):
    """探针必须显式跳过闸门，且条数仍是 min(5, 已取代数)——别顺手删探针换速度。"""
    brain.init_db(hub_root)
    for i in range(3):
        _supersede_pair(hub_root, i)
    calls = []
    real = brain.search_memories

    def spy(root, query="", kind="", limit=20, count_hits=True, semantic=True, gate=True):
        calls.append((query, semantic, gate))
        return real(root, query, kind, limit, count_hits=count_hits, semantic=semantic, gate=gate)

    brain.search_memories = spy
    try:
        r = ops._stale_health(hub_root)
    finally:
        brain.search_memories = real
    assert r["ok"] is True, r
    assert len(calls) == 3, f"3 条已取代应发 3 次探针：{calls}"
    assert all(sem is False and g is False for _, sem, g in calls), calls


def test_gate_off_skips_token_stats(hub_root):
    """省下的就是这 40ms：gate=False 一次 _token_stats 都不该发。"""
    brain.init_db(hub_root)
    _seed(hub_root)
    q = "紫色独角兽在冰川上跳舞"
    hits = []
    real = brain._token_stats

    def spy(root, tokens):
        hits.append(list(tokens))
        return real(root, tokens)

    brain._token_stats = spy
    try:
        brain.search_memories(hub_root, q, "", 10, count_hits=False, semantic=False)
        assert len(hits) >= 1, "gate=True 应会算 df（自检：探针没生效）"
        hits.clear()
        rows = brain.search_memories(hub_root, q, "", 10, count_hits=False,
                                     semantic=False, gate=False)
    finally:
        brain._token_stats = real
    assert hits == [], f"gate=False 仍在算 df：{hits}"
    assert rows, "gate=False 不该把结果清空"


def test_leak_still_reported_with_gate_off(hub_root, monkeypatch):
    """探针的目的是抓泄漏：管线真把已取代行吐回来时，gate=False 也必须报红。"""
    brain.init_db(hub_root)
    old, _ = _supersede_pair(hub_root, 1)
    with brain.db_conn(hub_root) as conn:
        leaked = dict(conn.execute("SELECT * FROM memories WHERE id=?", (old,)).fetchone())
    monkeypatch.setattr(brain, "_memories_search_fused",
                        lambda root, query, words, kind, limit, semantic: [dict(leaked)])
    r = ops._stale_health(hub_root)
    assert r["ok"] is False, r
    assert "泄漏" in r["summary"], r
    assert any(str(old) in d for d in r["detail"]), r["detail"]


def test_stale_health_probe_cap_is_five(hub_root):
    """抽查上限 5 条不变（不为凑数改口径）。"""
    brain.init_db(hub_root)
    for i in range(7):
        _supersede_pair(hub_root, i)
    n = []
    real = brain.search_memories
    brain.search_memories = lambda *a, **k: (n.append(1), real(*a, **k))[1]
    try:
        r = ops._stale_health(hub_root)
    finally:
        brain.search_memories = real
    assert r["ok"] is True, r
    assert len(n) == 5, f"探针条数应为 min(5, 7)=5，实得 {len(n)}"
