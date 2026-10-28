# -*- coding: utf-8 -*-
"""陈旧健康探针不再为一次抽查加载语义编码模型（R230）。

背景：R229 的耗时榜在真实库抓到 11.8s 全检里 stale_health 独占 9.1s——泄漏抽查走
search_memories 全融合四路，语义路 _ensure_emb 要加载/编码模型。体检是低频只读动作，
不该为「确认旧事实召不回」付模型加载钱。改为：词法/BM25/图路走 search_memories(semantic=False)
（端到端保留 coverage/fold 等后处理），语义路用同一 live 谓词的结构性计数覆盖——比抽样更强：
任何一条带向量的已取代行可达即报，不依赖查询相似度碰巧命中。

本文件守三件事：① 探针不再触达 _semantic_rank；② 泄漏仍抓得住；③ semantic 开关默认
不动普通检索（默认仍走语义路，能力不回退）。
"""
import brain
import ops


def _brain_with_superseded(root):
    brain.init_db(root)
    old = brain.add_memory(root, "旧结论：部署目录在 D:/old/path", kind="fact", agent="t")
    new = brain.add_memory(root, "新结论：部署目录在 D:/new/path", kind="fact", agent="t",
                           supersedes=old)
    return old, new


def test_probe_does_not_load_semantic_model(hub_root, monkeypatch):
    old, _ = _brain_with_superseded(hub_root)
    calls = []
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: calls.append(a) or [])
    r = ops.run_check("stale_health", "", hub_root)
    assert r["ok"], r
    assert not calls, "泄漏抽查仍走语义路：一次体检要加载编码模型（R229 实测 9.1s）"


def test_probe_still_catches_pipeline_leak(hub_root, monkeypatch):
    """把检索管线打回「会返回已取代条目」的原形，探针必须报泄漏（不能为了快把门拆了）。"""
    old, _ = _brain_with_superseded(hub_root)
    real = brain.search_memories

    def leaky(root, query="", kind="", limit=20, count_hits=True, semantic=True):
        rows = real(root, query, kind, limit, count_hits, semantic)
        if not any(r["id"] == old for r in rows):
            rows = rows + [{"id": old, "content": "旧结论"}]
        return rows

    monkeypatch.setattr(brain, "search_memories", leaky)
    r = ops.run_check("stale_health", "", hub_root)
    assert not r["ok"] and "泄漏" in r["summary"], r
    assert any(f"#{old}" in d for d in r["detail"]), r["detail"]


def test_live_filter_regression_is_caught(hub_root, monkeypatch):
    """模拟 _live_sql 丢掉 superseded 谓词：词法路会立刻召回旧行，必须报泄漏。"""
    old, _ = _brain_with_superseded(hub_root)
    monkeypatch.setattr(brain, "_live_sql", lambda a="": "1=1")
    r = ops.run_check("stale_health", "", hub_root)
    assert not r["ok"] and "泄漏" in r["summary"], r
    assert any(f"#{old}" in d for d in r["detail"]), r["detail"]


def test_semantic_flag_default_keeps_semantic_channel(hub_root, monkeypatch):
    brain.init_db(hub_root)
    brain.add_memory(hub_root, "语义通道默认仍在", kind="note", agent="t")
    calls = []
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: calls.append(a) or [])
    brain.search_memories(hub_root, "语义通道", count_hits=False)
    assert calls, "默认检索不再走语义路（能力回退）"
    brain.search_memories(hub_root, "语义通道", count_hits=False, semantic=False)
    assert len(calls) == 1, "semantic=False 没关掉语义路"
