# -*- coding: utf-8 -*-
r"""语义第三路先窄取后回表（R251）。

背景（真实库实测 output/_r251_sem_cost.txt）：`_semantic_rank` 每次 30.2–30.5ms，
其中 `SELECT e.eid, e.vec, r.*` 把 1575 条活跃记录的 content（971KB）连同 3.3MB
向量一起拉回，余弦过 0.58 门槛的却只有 0–11 条；余弦本身只 0.15ms。
R251 改为：窄取 `e.eid, e.vec` 算余弦 → 过阈值排序截 limit → 只对这些 id
回表取全列（6.1–12.8ms + 0.55–1.16ms，窄取随记录数增长仍慢于回表一个量级）。

本文件守四件事：
  ① 与 a13ae6b 的旧实现（`_semantic_rank_ref` 逐字保留，仅限定命名空间）在
     活行/门槛/limit/维度不符/并列上输出逐位相同；
  ② 被取代/过期/软删的行不因窄取换形态而复活；
  ③ 真正发出的 SQL：首查只取 `e.eid, e.vec`，回表 IN 参数 = 过阈值条数；
  ④ 两查之间行被取代 → 只会消失、不会复活（TOCTOU，同 R250 回表语义）。
"""
import numpy as np

import brain

DIM = 8
Q = np.zeros(DIM, dtype="float32")
Q[0] = 1.0


def _vec(c0, c1):
    v = np.zeros(DIM, dtype="float32")
    v[0], v[1] = c0, c1
    return v / (np.linalg.norm(v) + 1e-9)


def _seed(root, monkeypatch):
    """7 条记忆 + 3 条记录：活/被取代/过期/维度不符/门槛下侧/并列全都有。"""
    monkeypatch.setattr(brain, "_st_cls", lambda: None)   # 种库不落向量：手工写确定性向量
    brain.init_db(root)
    mids = [brain.add_memory(root, f"语义记忆{i}", kind="fact", agent="t") for i in range(7)]
    rids = [brain.add_record(root, "项目", "t", "2026-01-01", f"标题{i}", f"记录内容{i}")
            for i in range(3)]
    with brain.db_conn(root) as c:
        c.execute("UPDATE memories SET status='superseded' WHERE id=?", (mids[4],))
        c.execute("UPDATE memories SET valid_until='2020-01-01' WHERE id=?", (mids[5],))
        c.execute("UPDATE records SET superseded_by=? WHERE id=?", (rids[0], rids[1]))
    brain._init_emb_table(root)
    embs = {
        ("memories", mids[0]): _vec(1, 0),         # 1.0 过
        ("memories", mids[1]): _vec(0.8, 0.6),     # 0.8 过
        ("memories", mids[2]): _vec(0, 1),         # 0.0 不过
        ("memories", mids[3]): _vec(0.5, 0.866),   # 0.5 不过（门槛下侧）
        ("memories", mids[4]): _vec(1, 0),         # status=superseded
        ("memories", mids[5]): _vec(1, 0),         # 已过期
        ("memories", mids[6]): np.array([1, 0, 0], dtype="float32"),  # 维度不符
        ("records", rids[0]): _vec(0.9, 0.435),    # records 活行 0.9 过
        ("records", rids[1]): _vec(1, 0),          # 被 rids[0] 取代
        ("records", rids[2]): _vec(0.6, 0.8),      # 0.6 过
    }
    for (ent, eid), v in embs.items():
        brain._store_emb(root, ent, eid, v)
    return mids, rids


def _fake_encode(monkeypatch):
    """假编码器：所有 query 都得到 Q；记录调用次数，守住短路不加载模型。"""
    state = {"calls": 0}

    def enc(texts):
        state["calls"] += 1
        return np.stack([Q] * len(texts))

    monkeypatch.setattr(brain, "_ensure_emb", lambda: True)
    monkeypatch.setattr(brain, "_encode", enc)
    return state


def _semantic_rank_ref(root, query, entity, limit=100):
    """参照实现：a13ae6b 的 `_semantic_rank` 逐字保留（只把全局名限定到 brain.*）。"""
    if not query or not brain._SEM_CJK_RE.search(query):
        return []
    try:
        with brain.db_conn(root) as c:
            if c.execute("SELECT COUNT(*) FROM embeddings WHERE entity=?",
                         (entity,)).fetchone()[0] == 0:
                return []
    except Exception:
        return []
    if not brain._ensure_emb():
        return []
    qvec = brain._encode([query])
    if qvec is None:
        return []
    tbl = "records" if entity == "records" else "memories"
    live = brain._rec_live_sql("r.") if entity == "records" else brain._live_sql("r.")
    try:
        import numpy as np
        with brain.db_conn(root) as c:
            rows = c.execute(
                f"SELECT e.eid, e.vec, r.* FROM embeddings e JOIN {tbl} r ON e.eid=r.id "
                f"WHERE e.entity=? AND {live}", (entity,)).fetchall()
        if not rows:
            return []
        q = qvec[0].astype("float32")
        qn = np.linalg.norm(q) + 1e-9
        vecs, kept = [], []
        for row in rows:
            v = np.frombuffer(row["vec"], dtype="float32")
            if v.shape[0] != q.shape[0]:
                continue
            vecs.append(v); kept.append(row)
        if not vecs:
            return []
        M = np.stack(vecs)
        norms = np.linalg.norm(M, axis=1) + 1e-9
        sims = (M @ q) / (qn * norms)
        scored = []
        for row, sim in zip(kept, sims):
            if sim < brain._SEM_MIN_SIM:
                continue
            r = dict(row)
            r.pop("vec", None)
            r.pop("eid", None)
            scored.append((row["id"], float(sim), r))
        scored.sort(key=lambda t: -t[1])
        out = []
        for rank, (eid, sim, r) in enumerate(scored[:limit]):
            r["_semantic_sim"] = round(sim, 4)
            out.append((eid, 1.0 / (brain._RRF_K + rank + 1), r))
        return out
    except Exception:
        return []


def test_output_identical_to_pre_r251_reference(hub_root, monkeypatch):
    mids, rids = _seed(hub_root, monkeypatch)
    _fake_encode(monkeypatch)
    for entity in ("memories", "records"):
        for limit in (100, 2, 1):
            got = brain._semantic_rank(hub_root, "中文查询", entity, limit=limit)
            ref = _semantic_rank_ref(hub_root, "中文查询", entity, limit=limit)
            assert got == ref, (entity, limit, got, ref)
    got_ids = [eid for eid, _, _ in brain._semantic_rank(hub_root, "中文查询", "memories")]
    assert got_ids == [mids[0], mids[1]], got_ids          # 1.0 在前，0.8 在后
    rec_ids = [eid for eid, _, _ in brain._semantic_rank(hub_root, "中文查询", "records")]
    assert rec_ids == [rids[0], rids[2]], rec_ids


def test_threshold_and_live_rows(hub_root, monkeypatch):
    """门槛下侧 0.5、被取代、过期、维度不符一律不进；活行按余弦序返回。"""
    mids, rids = _seed(hub_root, monkeypatch)
    _fake_encode(monkeypatch)
    got = brain._semantic_rank(hub_root, "中文查询", "memories")
    assert [eid for eid, _, _ in got] == [mids[0], mids[1]]
    assert [r["_semantic_sim"] for _, _, r in got] == [1.0, 0.8]
    assert mids[3] not in {eid for eid, _, _ in got}       # 0.5 < 0.58
    assert mids[4] not in {eid for eid, _, _ in got}       # superseded
    assert mids[5] not in {eid for eid, _, _ in got}       # 过期
    assert mids[6] not in {eid for eid, _, _ in got}       # 维度不符
    recs = brain._semantic_rank(hub_root, "中文查询", "records")
    assert [eid for eid, _, _ in recs] == [rids[0], rids[2]]
    assert rids[1] not in {eid for eid, _, _ in recs}      # records 被取代行


def test_row_shape_matches_lexical_path(hub_root, monkeypatch):
    """行里不得漏出内部列（vec/eid），且保留全列——调用方按普通记忆行读 use_count/last_hit。"""
    mids, _ = _seed(hub_root, monkeypatch)
    _fake_encode(monkeypatch)
    _, rrf, row = brain._semantic_rank(hub_root, "中文查询", "memories")[0]
    assert "vec" not in row and "eid" not in row
    for key in ("id", "content", "kind", "status", "use_count", "last_hit"):
        assert key in row, (key, sorted(row))
    assert row["id"] == mids[0]
    assert rrf == 1.0 / (brain._RRF_K + 1)
    assert row["_semantic_sim"] == 1.0


def test_limit_applied_after_threshold(hub_root, monkeypatch):
    mids, _ = _seed(hub_root, monkeypatch)
    _fake_encode(monkeypatch)
    assert len(brain._semantic_rank(hub_root, "中文查询", "memories", limit=1)) == 1
    assert brain._semantic_rank(hub_root, "中文查询", "memories", limit=0) == []


class _SpyConn:
    """包住 db_conn 的真连接，记录语句；before 钩子可在回表前改库（TOCTOU 用）。"""

    def __init__(self, cm, sink, before=None):
        self._cm, self._sink, self._before, self._c = cm, sink, before, None

    def __enter__(self):
        self._c = self._cm.__enter__()
        return self

    def __exit__(self, *a):
        return self._cm.__exit__(*a)

    def execute(self, sql, params=()):
        self._sink.append((sql, tuple(params)))
        if self._before is not None:
            self._before(self._c, sql, params)
        return self._c.execute(sql, params)


def _spy(monkeypatch, sink, before=None):
    real = brain.db_conn
    monkeypatch.setattr(brain, "db_conn",
                        lambda root, *a, **k: _SpyConn(real(root, *a, **k), sink, before))


def test_sql_shape_narrow_then_refetch(hub_root, monkeypatch):
    """动机本身：首查只取 eid+vec（不许 r.*），回表 IN 参数 = 过阈值条数。"""
    mids, _ = _seed(hub_root, monkeypatch)
    _fake_encode(monkeypatch)
    sink = []
    _spy(monkeypatch, sink)
    out = brain._semantic_rank(hub_root, "中文查询", "memories")
    assert [eid for eid, _, _ in out] == [mids[0], mids[1]]
    joins = [s for s, _ in sink if "JOIN memories" in s]
    refetch = [(s, p) for s, p in sink if s.startswith("SELECT * FROM memories")]
    assert len(joins) == 1 and "SELECT e.eid, e.vec FROM embeddings" in joins[0], sink
    assert "r.*" not in joins[0], joins[0]
    assert len(refetch) == 1, sink
    assert "id IN (?,?)" in refetch[0][0] and len(refetch[0][1]) == 2, refetch
    assert "status='active'" in refetch[0][0], refetch   # 回表仍带存活谓词


def test_vanished_row_between_queries_only_disappears(hub_root, monkeypatch):
    """回表前把一条已过阈值行改成 superseded：它必须消失，其余行排名分不变。"""
    mids, _ = _seed(hub_root, monkeypatch)
    _fake_encode(monkeypatch)
    baseline = brain._semantic_rank(hub_root, "中文查询", "memories")
    victim = mids[0]

    def before(conn, sql, params):
        if sql.startswith("SELECT * FROM memories"):
            conn.execute("UPDATE memories SET status='superseded' WHERE id=?", (victim,))

    sink = []
    _spy(monkeypatch, sink, before=before)
    raced = brain._semantic_rank(hub_root, "中文查询", "memories")
    assert [t for t in baseline if t[0] != victim] == raced
    assert victim not in {eid for eid, _, _ in raced}
    assert [r["_semantic_sim"] for _, _, r in raced] == [0.8]   # 排名分保留原 rank


def test_short_circuits_skip_model(hub_root, monkeypatch):
    """无向量表 / 非中文 / 编码失败：三条短路必须不碰模型，且 fail-open。"""
    _seed(hub_root, monkeypatch)
    state = _fake_encode(monkeypatch)
    assert brain._semantic_rank(hub_root, "中文查询", "ghost") == []
    assert brain._semantic_rank(hub_root, "graph query", "memories") == []
    assert state["calls"] == 0, state
    monkeypatch.setattr(brain, "_encode", lambda texts: None)
    assert brain._semantic_rank(hub_root, "中文查询", "memories") == []


def test_fail_open_on_missing_root(tmp_path, monkeypatch):
    _fake_encode(monkeypatch)
    assert brain._semantic_rank(str(tmp_path / "never-created"), "中文查询", "memories") == []
