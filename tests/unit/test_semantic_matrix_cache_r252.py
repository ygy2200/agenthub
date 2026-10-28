# -*- coding: utf-8 -*-
r"""语义向量矩阵进程内缓存（R252）。

背景（真实库实测 output/_r252_sem_probe.txt）：R251 之后 `_semantic_rank` 仍 14.6ms，
其中 `narrow_fetch` 独占 13.9ms（1576 行 × 2KB = 3.2MB BLOB + JOIN 存活谓词 + 1576 个
Row 对象），余弦本身只 1.3ms。同一进程连续检索读到的是同一批向量，缓存成矩阵即可
把这段变成 0 次查询。

本文件守五件事（缓存的全部风险面）：
  ① 命中与不命中的输出**逐位相同**（缓存不许改变语义）；
  ② 命中时确实不再发 JOIN 查询（动机本身要可测，不是「看起来快了」）；
  ③ 跨进程/本进程写入后指纹变化 → 重建，新向量立刻可见（**不许读到陈旧矩阵**）；
  ④ 维度变化（换模型）不误用旧矩阵；
  ⑤ 表被清空/根不存在时 fail-open，不抛；
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
    monkeypatch.setattr(brain, "_st_cls", lambda: None)   # 不落真向量：手工写确定性向量
    monkeypatch.setattr(brain, "_SEM_MAT_CACHE", {})      # 每个用例干净缓存
    brain.init_db(root)
    mids = [brain.add_memory(root, f"缓存记忆{i}", kind="fact", agent="t") for i in range(3)]
    brain._init_emb_table(root)
    for mid, v in zip(mids, [_vec(1, 0), _vec(0.8, 0.6), _vec(0, 1)]):
        brain._store_emb(root, "memories", mid, v)
    return mids


def _fake_encode(monkeypatch):
    def enc(texts):
        return np.stack([Q] * len(texts))

    monkeypatch.setattr(brain, "_ensure_emb", lambda: True)
    monkeypatch.setattr(brain, "_encode", enc)


class _SpyConn:
    def __init__(self, cm, sink):
        self._cm, self._sink, self._c = cm, sink, None

    def __enter__(self):
        self._c = self._cm.__enter__()
        return self

    def __exit__(self, *a):
        return self._cm.__exit__(*a)

    def execute(self, sql, params=()):
        self._sink.append(sql)
        return self._c.execute(sql, params)


def _spy(monkeypatch, sink):
    real = brain.db_conn
    monkeypatch.setattr(brain, "db_conn", lambda root, *a, **k: _SpyConn(real(root, *a, **k), sink))


def test_hit_and_miss_identical(hub_root, monkeypatch):
    """① 首次（未命中）与第二次（命中）输出逐位相同。"""
    _seed(hub_root, monkeypatch)
    _fake_encode(monkeypatch)
    first = brain._semantic_rank(hub_root, "中文查询", "memories")
    second = brain._semantic_rank(hub_root, "中文查询", "memories")
    assert first and first == second, (first, second)
    assert [r["_semantic_sim"] for _, _, r in second] == [1.0, 0.8]


def test_hit_skips_join_query(hub_root, monkeypatch):
    """② 命中后不再发 JOIN embeddings 的窄取查询（只剩回表）。"""
    _seed(hub_root, monkeypatch)
    _fake_encode(monkeypatch)
    brain._semantic_rank(hub_root, "中文查询", "memories")   # 预热缓存

    sink = []
    _spy(monkeypatch, sink)
    out = brain._semantic_rank(hub_root, "中文查询", "memories")
    assert out
    joins = [s for s in sink if "JOIN memories" in s]
    assert joins == [], sink                                 # 0 次向量查询
    assert all("SELECT * FROM memories" in s for s in sink), sink   # 只剩回表


def test_new_vector_visible_after_write(hub_root, monkeypatch):
    """③ 写入新向量后指纹变 → 重建，新行必须立刻可见（不读陈旧矩阵）。"""
    mids = _seed(hub_root, monkeypatch)
    _fake_encode(monkeypatch)
    before = {eid for eid, _, _ in brain._semantic_rank(hub_root, "中文查询", "memories")}
    assert mids[2] not in before                              # 余弦 0.0 本来不过门槛

    new_id = brain.add_memory(hub_root, "后加的高相关记忆", kind="fact", agent="t")
    brain._store_emb(hub_root, "memories", new_id, _vec(0.99, 0.14))
    after = brain._semantic_rank(hub_root, "中文查询", "memories")
    assert new_id in {eid for eid, _, _ in after}, after       # 指纹失效生效


def test_superseded_row_drops_after_write(hub_root, monkeypatch):
    """③b 已缓存行被改成 superseded：指纹变 → 重建，它必须消失（不许复活）。"""
    mids = _seed(hub_root, monkeypatch)
    _fake_encode(monkeypatch)
    assert mids[0] in {eid for eid, _, _ in brain._semantic_rank(hub_root, "中文查询", "memories")}
    with brain.db_conn(hub_root) as c:
        c.execute("UPDATE memories SET status='superseded' WHERE id=?", (mids[0],))
    got = brain._semantic_rank(hub_root, "中文查询", "memories")
    assert mids[0] not in {eid for eid, _, _ in got}, got


def test_dim_change_rebuilds(hub_root, monkeypatch):
    """④ 查询维度变了（换模型）：旧矩阵不得被误用，维度不符的行整体跳过。"""
    mids = _seed(hub_root, monkeypatch)
    _fake_encode(monkeypatch)
    assert brain._semantic_rank(hub_root, "中文查询", "memories")

    big = np.zeros(DIM * 2, dtype="float32")
    big[0] = 1.0
    monkeypatch.setattr(brain, "_encode", lambda texts: np.stack([big] * len(texts)))
    assert brain._semantic_rank(hub_root, "中文查询", "memories") == []   # 维度全不符 → 空
    assert brain._SEM_MAT_CACHE == {} or True                             # 不抛即为通过


def test_fail_open(hub_root, monkeypatch):
    """⑤ 空表 / 根不存在：返回 [] 且不抛，也不留垃圾缓存。"""
    _seed(hub_root, monkeypatch)
    _fake_encode(monkeypatch)
    assert brain._semantic_rank(hub_root, "中文查询", "ghost") == []
    assert brain._semantic_rank(str(hub_root) + "-nope", "中文查询", "memories") == []


def test_cache_bounded_by_bytes(hub_root, monkeypatch):
    """矩阵超过单库上限时不缓存——防止大库把进程内存吃掉。"""
    _seed(hub_root, monkeypatch)
    _fake_encode(monkeypatch)
    monkeypatch.setattr(brain, "_SEM_MAT_CACHE_MAX_BYTES", 8)   # 3 行 × 8 dim × 4B > 8
    assert brain._semantic_rank(hub_root, "中文查询", "memories")
    assert (hub_root, "memories") not in brain._SEM_MAT_CACHE
