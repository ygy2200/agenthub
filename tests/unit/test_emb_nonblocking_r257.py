# -*- coding: utf-8 -*-
r"""语义模型加载不再阻塞读路径（R257）。

实测（`output/_r257_slow_query.txt`，真实库 426 记忆 / 1597 记录）：`eval_search.py`
报 `max 9286.5ms` 而 p50 只 72.6ms；逐条计时后确认 **9.3 秒全部落在 warmup**——
`_ensure_emb()` 首次调用要 9190ms（torch + transformers 导入 + 权重加载），
单条查询最慢也只 95.7ms。用户感知就是「第一次搜中文卡死」。

R257 把**读路径**改成不等：`_emb_ready_now()` 起 daemon 线程慢慢装、本次返回 False
（词法两路 + 图路照常出结果），装完下次检索自动带上语义路。写路径必须仍阻塞——
向量要当场落库，否则新数据在语义路上长期缺席，那比慢 9 秒更糟。

本文件守四件事：
  ① 未就绪时 `_semantic_rank` **立刻**返回（不阻塞、不抛）；
  ② 后台只起一个线程（重复检索不重复装）；
  ③ 装完后同一个进程里的检索真的用上语义路（不是永久降级）；
  ④ 写路径仍阻塞并落向量（`add_memory` 后 embeddings 有行）。
"""
import threading
import time

import numpy as np
import pytest

import brain

DIM = 8
Q = np.zeros(DIM, dtype="float32")
Q[0] = 1.0
LOAD_SECONDS = 0.4


def _vec(c0, c1):
    v = np.zeros(DIM, dtype="float32")
    v[0], v[1] = c0, c1
    return v / (np.linalg.norm(v) + 1e-9)


class _SlowModel:
    """假模型：构造时故意慢，模拟 9.2s 的 torch/权重加载。"""

    calls = 0

    def __init__(self, name, local_files_only=False):
        type(self).calls += 1
        time.sleep(LOAD_SECONDS)

    def get_embedding_dimension(self):
        return DIM

    def encode(self, texts, normalize_embeddings=True, convert_to_numpy=True):
        return np.stack([Q] * len(texts))


@pytest.fixture
def fresh_emb(monkeypatch):
    """重置模块级模型状态（其它用例可能已把它置 True/False）。"""
    monkeypatch.setattr(brain, "_EMB_READY", None)
    monkeypatch.setattr(brain, "_EMB", None)
    monkeypatch.setattr(brain, "_EMB_DIM", None)
    monkeypatch.setattr(brain, "_EMB_BG_THREAD", None)
    monkeypatch.setattr(brain, "_st_cls", lambda: _SlowModel)
    _SlowModel.calls = 0
    yield
    # 等后台线程收尾，避免污染后续用例的 _EMB_READY
    t = brain._EMB_BG_THREAD
    if t is not None and t.is_alive():
        t.join(timeout=5)


def _seed(root, monkeypatch):
    monkeypatch.setattr(brain, "_SEM_MAT_CACHE", {})
    brain.init_db(root)
    mids = [brain.add_memory(root, f"后台加载记忆{i}", kind="fact", agent="t") for i in range(2)]
    brain._init_emb_table(root)
    for mid, v in zip(mids, [_vec(1, 0), _vec(0.9, 0.44)]):
        brain._store_emb(root, "memories", mid, v)
    # 关键：add_memory 走写路径已经把模型装好了（那正是设计要求），这里要测的是
    # 「读路径第一次遇到未加载」——所以数据种完后把模型状态打回未尝试。
    monkeypatch.setattr(brain, "_EMB_READY", None)
    monkeypatch.setattr(brain, "_EMB", None)
    monkeypatch.setattr(brain, "_EMB_BG_THREAD", None)
    _SlowModel.calls = 0
    return mids


def test_read_path_does_not_block(hub_root, monkeypatch, fresh_emb):
    """① 未就绪：立刻返回空（远快于加载耗时），且不抛。"""
    _seed(hub_root, monkeypatch)
    t0 = time.perf_counter()
    out = brain._semantic_rank(hub_root, "中文查询", "memories")
    elapsed = time.perf_counter() - t0
    assert out == []
    assert elapsed < LOAD_SECONDS / 2, f"读路径仍在等模型加载：{elapsed:.2f}s"
    assert brain._EMB_BG_THREAD is not None and brain._EMB_BG_THREAD.daemon


def test_only_one_background_load(hub_root, monkeypatch, fresh_emb):
    """② 连续多次检索只起一个线程（不重复装模型）。"""
    _seed(hub_root, monkeypatch)
    for _ in range(5):
        brain._semantic_rank(hub_root, "中文查询", "memories")
    t = brain._EMB_BG_THREAD
    t.join(timeout=5)
    assert _SlowModel.calls == 1, _SlowModel.calls


def test_semantic_path_returns_after_load(hub_root, monkeypatch, fresh_emb):
    """③ 装完就绪后，同一进程的检索真的用上语义路（不是永久降级）。"""
    mids = _seed(hub_root, monkeypatch)
    assert brain._semantic_rank(hub_root, "中文查询", "memories") == []   # 触发后台加载
    brain._EMB_BG_THREAD.join(timeout=5)
    assert brain._EMB_READY is True, brain._EMB_READY
    got = brain._semantic_rank(hub_root, "中文查询", "memories")
    assert [eid for eid, _, _ in got] == mids, got      # 余弦 1.0 / 0.9 都过门槛


def test_write_path_still_blocks_and_embeds(hub_root, monkeypatch, fresh_emb):
    """④ 写路径必须等模型就位并落向量——否则新数据在语义路上永久缺席。"""
    brain.init_db(hub_root)
    brain._init_emb_table(hub_root)
    mid = brain.add_memory(hub_root, "写入就该有向量", kind="fact", agent="t")
    assert brain._EMB_READY is True, "写路径没有等待模型就绪"
    with brain.db_conn(hub_root) as c:
        n = c.execute("SELECT COUNT(*) FROM embeddings WHERE entity='memories' AND eid=?",
                      (mid,)).fetchone()[0]
    assert n == 1, n
