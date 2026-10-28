# -*- coding: utf-8 -*-
r"""R244·相似度全量扫描复用的三条不变量：命中不改结果、语料一变必失效、缓存不许被写脏。

背景（真实库实测，output/_r244_dup_cost.txt）：`similar_memories` 是 O(N²) 全量扫描，
388 条现行记忆 / 19 万字 = 120ms（分词 36 + 位集 21 + 对比 63），而**一次会话里同一份
语料被五处各扫一遍**：治理页 consolidate 段（limit=10）、安全段 dup_mem 巡检（10）、
统计页 health_report（20）、metacognition dup_pairs（20）、ghost_memory（50）。
R244 加了两层复用：结果缓存 `_SIM_CACHE`（键 = 语料快照 + threshold + cap）与分词复用
（走既有 `_TOK_CACHE` 的 sim 命名空间）。命中 2ms。

复用的风险全在**失效**：漏掉一条写路径，就会拿旧配对当新配对摆在屏幕上（数据库里
看得见、界面里看不见）。所以本文件的重点是「每种语料变化都让缓存失效」，不是「第二次快」。
"""
import brain
import pytest

TEXT_A = "超时重试要指数退避，最多三次，别用固定间隔"
TEXT_B = "超时重试要指数退避，最多三次，别用固定间隔。"


@pytest.fixture(autouse=True)
def _clean_caches():
    """缓存是进程级的：用例之间必须清空，否则先跑的用例会把语料喂给后跑的。"""
    brain._SIM_CACHE.clear()
    brain._TOK_CACHE.clear()
    yield
    brain._SIM_CACHE.clear()
    brain._TOK_CACHE.clear()


def _seed(root):
    brain.init_db(root)
    a = brain.add_memory(root, TEXT_A, kind="lesson", agent="t")
    b = brain.add_memory(root, TEXT_B, kind="lesson", agent="t")
    brain.add_memory(root, "完全不相干的一条：发布节奏按里程碑走", kind="fact", agent="t")
    return a, b


def _scan_count(monkeypatch):
    """数一数真扫了几次：命中与否不看耗时（耗时在 CI 上不稳），看有没有走到扫描本体。"""
    calls = []
    real = brain._similar_scan

    def spy(root, corpus, threshold, cap):
        calls.append(len(corpus))
        return real(root, corpus, threshold, cap)

    monkeypatch.setattr(brain, "_similar_scan", spy)
    return calls


def test_hit_does_not_rescan_and_every_write_invalidates(hub_root, monkeypatch):
    a, b = _seed(hub_root)
    calls = _scan_count(monkeypatch)
    first = brain.similar_memories(hub_root, 0.55, 10)
    assert len(calls) == 1
    assert first and first[0]["a"] == a and first[0]["b"] == b, first

    assert brain.similar_memories(hub_root, 0.55, 10) == first
    assert len(calls) == 1, "语料没变却又扫了一遍——缓存没命中"

    # ① 增行
    brain.add_memory(hub_root, "新增一条", kind="fact", agent="t")
    brain.similar_memories(hub_root, 0.55, 10)
    assert len(calls) == 2, "插入新记忆后仍命中旧结果"

    # ② 就地改写正文（edit_memory）
    assert brain.edit_memory(hub_root, b, content="改成了另一段话，和上一条不再重复",
                             agent="t") == ""
    brain.similar_memories(hub_root, 0.55, 10)
    assert len(calls) == 3, "正文改了仍命中旧结果"

    # ③ supersede（旧条退出现行集）
    assert brain.supersede_memory(hub_root, a, b) == ""
    brain.similar_memories(hub_root, 0.55, 10)
    assert len(calls) == 4, "记忆被取代后仍命中旧结果"

    # ④ consolidate_apply 那类「只改 superseded_by、不写 updated」的写路径
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE memories SET superseded_by=0 WHERE id=?", (a,))
    brain.similar_memories(hub_root, 0.55, 10)
    assert len(calls) == 5, "复活一条记忆后仍命中旧结果"


def test_same_second_equal_length_rewrite_still_invalidates(hub_root, monkeypatch):
    """间接签名最容易漏的一种：**同一秒内**把正文换成等长的另一段。

    这条正是「用 MAX(updated) + SUM(LENGTH(content)) 当签名」会漏掉的形态（秒级时间戳
    不变、总长度不变），所以缓存键直接认语料本身，不认这些间接量。
    """
    a, _ = _seed(hub_root)
    calls = _scan_count(monkeypatch)
    brain.similar_memories(hub_root, 0.55, 10)
    assert len(calls) == 1

    new_text = ("缓存命中判据必须是语料本身，不能是时间戳" * 2)[:len(TEXT_A)]
    assert len(new_text) == len(TEXT_A) and new_text != TEXT_A, "用例前提：等长改写"
    with brain.db_conn(hub_root) as conn:
        old = conn.execute("SELECT updated FROM memories WHERE id=?", (a,)).fetchone()[0]
        conn.execute("UPDATE memories SET content=?, updated=? WHERE id=?", (new_text, old, a))
    brain.similar_memories(hub_root, 0.55, 10)
    assert len(calls) == 2, "等长改写 + 同一秒 → 缓存把旧正文的结果端了出来"


def test_hit_returns_copies(hub_root):
    """ghost_memory_candidates 会就地给条目补 suggestion——共享 dict 会把缓存写脏。"""
    _seed(hub_root)
    first = brain.similar_memories(hub_root, 0.55, 10)
    assert first, "用例前提：种下的两条要真的被判为重复"
    first[0]["suggestion"] = "污染"
    again = brain.similar_memories(hub_root, 0.55, 10)
    assert "suggestion" not in again[0], again[0]


def test_token_cache_is_reused_across_limits(hub_root):
    """换 limit 重扫时不该再分词一遍：词集在 sim 命名空间里。"""
    a, b = _seed(hub_root)
    brain.similar_memories(hub_root, 0.55, 10)
    cached = brain._TOK_CACHE.sim_items()
    assert (hub_root, a) in cached and (hub_root, b) in cached, cached.keys()
    assert cached[(hub_root, a)] == brain._tokens(TEXT_A)   # 与 similar_lessons_for 同值语义

    brain._SIM_CACHE.clear()          # 只清结果缓存，模拟换 limit
    brain.similar_memories(hub_root, 0.55, 50)
    assert brain._TOK_CACHE.sim_items()[(hub_root, b)] == brain._tokens(TEXT_B)


def test_edit_and_rollback_invalidate_token_cache(hub_root):
    """rollback 恢复的是旧正文：分词缓存必须跟着作废（R244 补上的漏点）。"""
    a, _ = _seed(hub_root)
    brain.similar_memories(hub_root, 0.55, 10)
    assert (hub_root, a) in brain._TOK_CACHE.sim_items()

    assert brain.edit_memory(hub_root, a, content="改过的正文", agent="t") == ""
    assert (hub_root, a) not in brain._TOK_CACHE.sim_items(), "edit_memory 没让分词缓存失效"

    rev = brain.memory_revisions(hub_root, a)
    assert rev, "edit_memory 应当留下一条修订"
    brain.similar_memories(hub_root, 0.55, 10)          # 重新灌进缓存
    assert (hub_root, a) in brain._TOK_CACHE.sim_items()
    assert brain.memory_rollback(hub_root, a, rev[0]["id"], agent="t") == ""
    assert (hub_root, a) not in brain._TOK_CACHE.sim_items(), "回滚没让分词缓存失效"
    with brain.db_conn(hub_root) as conn:
        assert conn.execute("SELECT content FROM memories WHERE id=?", (a,)).fetchone()[0] == TEXT_A


def test_sim_namespace_is_bounded(hub_root, monkeypatch):
    """sim 命名空间此前没有上限；改为全库扫描写入后，常驻进程需要与 BM25 同款总量兜底。"""
    monkeypatch.setattr(brain, "_TOK_CACHE_MAX", 3)
    for i in range(6):
        brain._TOK_CACHE.put_sim("r", i, {"x"})
    assert len(brain._TOK_CACHE.sim_items()) == 3
