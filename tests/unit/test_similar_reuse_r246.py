# -*- coding: utf-8 -*-
r"""R246·相似度复用从「同参命中」放宽到「可派生」，以及随之修掉的一处口径漏洞。

背景（真实库实测，output/_r246_probe2.txt）：一次巡检里 `similar_memories` 对**同一份语料**
扫了两遍——`ghost_memory` 问 (0.55, cap=50) 110.0ms，`dup_mem` 问 (0.55, cap=10) 73.6ms。
R244 的缓存键含 cap，于是谁都没命中谁。

R246 的规则：一次**未触 cap** 的扫描是所有「阈值≥它、上限≥其结果条数」请求的超集——
按阈值筛子集、按上限截断都只是取子集，结果逐字等价。反过来有两条硬边界，也是本文件的
主要断言：
  ① 触过 cap 的扫描只能同参复用：截断发生在**扫描序**上（扫满 cap 条就 return，之后才排序），
     事后按 sim 截断得到的不是同一批对；
  ② 缓存阈值高于本次请求时必须重扫：低阈值的对根本不在缓存里，补不出来。

记录侧（`similar_records`）同款复用 + 顺手修 ⑦：原实现嘴上比 `record_type`，SELECT 里却没有
这一列，`.get(..., "")` 两边同为 "" → 每个记录对都被判「同类」→ 跨类型对误标「疑似重复」。
"""
import pytest

import brain

A1 = "发布节奏按里程碑走，隔几个大版本才发一次 Release"
A2 = "发布节奏按里程碑走，隔几个大版本才发一次 Release，其余只推 main 和 tag"
B1 = "数据库备份保留最近七天，过期文件由 prune 清理"
B2 = "数据库备份保留最近七天，过期文件由 prune 清理，清理前先确认"
C = "液压传动考试复习要点整理成 Word 文档，含选择题与计算题解析"
D = "根目录指错了会导致自检报告库不存在，先用 --root 指回真实根目录"

TODAY = "2026-10-28"


@pytest.fixture(autouse=True)
def _clean_caches():
    """三个缓存都是进程级的，必须逐用例清空（先跑的语料会喂给后跑的）。"""
    for name in ("_SIM_CACHE", "_REC_CACHE", "_TOK_CACHE"):
        getattr(brain, name).clear()
    yield
    for name in ("_SIM_CACHE", "_REC_CACHE", "_TOK_CACHE"):
        getattr(brain, name).clear()


def _seed_memories(root):
    brain.init_db(root)
    for text, kind in ((A1, "project"), (A2, "project"), (B1, "lesson"), (B2, "lesson"),
                       (C, "note"), (D, "lesson")):
        brain.add_memory(root, text, kind=kind, agent="t")


def _seed_records(root):
    brain.init_db(root)
    brain.add_record(root, "甲-项目", "t", TODAY, "发布节奏定档",
                     A1 + "。【验证结果】推到 main 与 tag")
    brain.add_record(root, "甲-项目", "t", TODAY, "发布节奏定档",
                     A2 + "。【验证结果】推到 main 与 tag")
    brain.add_record(root, "乙-项目", "t", TODAY, "备份保留策略",
                     B1 + "。【验证结果】prune 生效")
    brain.add_record(root, "乙-项目", "t", TODAY, "备份保留策略",
                     B2 + "。【验证结果】prune 生效")


def _cold_mem(root, thr, cap):
    brain._SIM_CACHE.clear()
    return brain.similar_memories(root, thr, cap)


def _cold_rec(root, thr, cap, **kw):
    brain._REC_CACHE.clear()
    return brain.similar_records(root, thr, cap, **kw)


def _keys(pairs):
    """比较用的稳定视图：顺序、配对、取整后的 sim（这就是调用方能观察到的全部）。"""
    return [(p["a"], p["b"], p["sim"]) for p in pairs]


def _count(monkeypatch, attr):
    calls = []
    real = getattr(brain, attr)

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(brain, attr, spy)
    return calls


def test_derived_equals_direct_across_threshold_and_cap(hub_root, monkeypatch):
    """一次未触上限的低阈值扫描，必须能逐字服务所有更高阈值/更多上限的请求。"""
    _seed_memories(hub_root)
    matrix = [(thr, cap) for thr in (0.3, 0.55, 0.8, 0.95) for cap in (5, 10, 50)]
    truth = {cell: _keys(_cold_mem(hub_root, *cell)) for cell in matrix}
    n_pairs = len(truth[(0.3, 50)])
    assert 0 < n_pairs < 5, f"用例前提：语料要产出 1~4 对相似，且小于最小上限；实际 {n_pairs}"

    brain._SIM_CACHE.clear()
    calls = _count(monkeypatch, "_similar_scan")
    warm = _keys(brain.similar_memories(hub_root, 0.3, 50))
    assert len(calls) == 1, "首次低阈值大上限应真扫一次"
    assert warm == truth[(0.3, 50)]

    for cell in matrix:
        got = _keys(brain.similar_memories(hub_root, *cell))
        assert got == truth[cell], f"{cell} 派生结果与直扫不等"
    assert len(calls) == 1, "语料没变却重扫了——派生规则没生效"


def test_truncated_scan_is_only_reused_with_same_params(hub_root, monkeypatch):
    """触过 cap 的扫描不能派生：截断发生在扫描序上，事后按 sim 截断不是同一批对。"""
    brain.init_db(hub_root)
    for n in range(12):
        brain.add_memory(hub_root, f"巡检第 {n} 轮记录：三层门禁跑完，部署一致性核对通过",
                         kind="project", agent="t")
    truth10 = _keys(_cold_mem(hub_root, 0.55, 10))

    calls = _count(monkeypatch, "_similar_scan")
    short = _keys(brain.similar_memories(hub_root, 0.55, 2))
    assert len(calls) == 1 and len(short) == 2, "用例前提：cap=2 要被截断"

    again = _keys(brain.similar_memories(hub_root, 0.55, 2))
    assert len(calls) == 1, "同参请求应命中截断缓存（逐字等价，不需要完整）"
    assert again == short

    got = _keys(brain.similar_memories(hub_root, 0.55, 10))
    assert len(calls) == 2, "从截断结果派生了更大上限的请求"
    assert got == truth10, "重扫结果与直扫不等"


def test_derivation_edges(hub_root, monkeypatch):
    """两条边界：缓存阈值更高要重扫；本次会截断在扫描序上也要重扫（哪怕缓存完整）。"""
    _seed_memories(hub_root)
    truth_low = _keys(_cold_mem(hub_root, 0.55, 50))
    n_pairs = len(truth_low)
    assert 0 < n_pairs < 50, n_pairs

    brain._SIM_CACHE.clear()      # 取真值时顺带填了缓存，这里要的是冷启动
    calls = _count(monkeypatch, "_similar_scan")
    brain.similar_memories(hub_root, 0.8, 50)
    assert len(calls) == 1
    lower = _keys(brain.similar_memories(hub_root, 0.55, 50))
    assert len(calls) == 2, "缓存阈值 0.8 高于请求 0.55，必须重扫（低阈值的对不在缓存里）"
    assert lower == truth_low

    # 此时缓存 = (0.55, 50, 完整)。请求上限恰好等于结果条数 → 会截断在扫描序上
    exact_cap = _keys(brain.similar_memories(hub_root, 0.55, n_pairs))
    assert len(calls) == 3, f"结果条数({n_pairs})恰好等于本次上限时不许派生"
    assert len(exact_cap) == n_pairs
    assert {p[:2] for p in exact_cap} == {p[:2] for p in truth_low}

    bigger = _keys(brain.similar_memories(hub_root, 0.55, 50))
    assert len(calls) == 4, "上一格是截断扫描（不完整），回大上限必须重扫"
    assert bigger == truth_low


def test_records_cache_hits_and_invalidates(hub_root, monkeypatch):
    _seed_records(hub_root)
    truth = _keys(_cold_rec(hub_root, 0.55, 20))
    assert truth, "用例前提：种下的记录对要能被判为相似"

    brain._REC_CACHE.clear()      # 取真值时顺带填了缓存，这里要的是冷启动
    calls = _count(monkeypatch, "_records_scan")
    assert _keys(brain.similar_records(hub_root, 0.55, 20)) == truth
    assert len(calls) == 1
    assert _keys(brain.similar_records(hub_root, 0.55, 20)) == truth
    assert len(calls) == 1, "语料没变却重扫了"

    # 不同 project / days 是不同的语料键，互不串味
    brain.similar_records(hub_root, 0.55, 20, project="甲-项目")
    brain.similar_records(hub_root, 0.55, 20, days=7)
    assert len(calls) == 3, "项目过滤与天数窗口应各自真扫一次"

    brain.add_record(hub_root, "甲-项目", "t", TODAY, "新增一条发布节奏",
                     A1 + "，另外补了回归脚本。【验证结果】推送成功")
    got = _keys(brain.similar_records(hub_root, 0.55, 20))
    assert len(calls) == 4, "记录增行后仍命中旧结果"
    assert got == _keys(_cold_rec(hub_root, 0.55, 20))


def test_records_derivation_matrix(hub_root, monkeypatch):
    _seed_records(hub_root)
    matrix = [(0.3, 10), (0.3, 50), (0.55, 10), (0.55, 50), (0.9, 20)]
    truth = {cell: _keys(_cold_rec(hub_root, *cell)) for cell in matrix}
    assert truth[(0.55, 10)], "用例前提：记录对要能被判为相似"
    assert len(truth[(0.3, 50)]) < 10, truth[(0.3, 50)]

    brain._REC_CACHE.clear()
    calls = _count(monkeypatch, "_records_scan")
    warm = _keys(brain.similar_records(hub_root, 0.3, 50))
    assert len(calls) == 1 and warm == truth[(0.3, 50)]

    for cell in matrix:
        got = _keys(brain.similar_records(hub_root, *cell))
        assert got == truth[cell], f"{cell} 派生结果与直扫不等"
    assert len(calls) == 1, "记录侧派生规则没生效"


def test_records_verdict_reads_record_type(hub_root):
    """⑦：跨类型对不能再被标成「疑似重复」——record_type 得真的读出来。

    分两个项目各放一对，避免两对互相干扰；判定只认项目——同项目同类型对、
    跨项目跨类型对，各自的 verdict 一眼可辨。
    """
    brain.init_db(hub_root)
    brain.add_record(hub_root, "甲-项目", "t", TODAY, "同文同类型", A1, record_type="work_log")
    brain.add_record(hub_root, "甲-项目", "t", TODAY, "同文同类型", A1, record_type="work_log")
    brain.add_record(hub_root, "乙-项目", "t", TODAY, "同文跨类型", B1, record_type="decision")
    brain.add_record(hub_root, "乙-项目", "t", TODAY, "同文跨类型", B1, record_type="work_log")

    pairs = brain.similar_records(hub_root, 0.8, 50)
    assert len(pairs) == 2, pairs
    got = {p["project"]: p["verdict"] for p in pairs}
    assert got == {"甲-项目": "疑似重复", "乙-项目": "相关"}, got


def test_tokens_findall_matches_legacy():
    """R246 换了归一化写法（sub+split → findall），必须逐词集等价，含非中日的 \\w 文字。"""
    import re
    word = re.compile(r"[a-z0-9]+")

    def legacy(text):
        text = re.sub(r"[^\w一-鿿]+", " ", (text or "").lower())
        toks = set()
        for seg in text.split():
            if word.fullmatch(seg):
                toks.add(seg)
            else:
                toks.update(seg[i:i + 2] for i in range(len(seg) - 1))
        return toks

    samples = ["abc-def", "a_b", "中文【目的】x", "にほんご カナ", "Привет мир", "",
               "!!!", "ＡＢＣ　１２３", "CamelCase 混合 Text", "a1b2c3", "一",
               "多 空格   分段", "emoji🙂后接中文"]
    for s in samples:
        assert brain._tokens(s) == legacy(s), s
    assert brain._tokens("") == brain._tokens(None) == set()


def test_thaw_copies_and_records_cache_bounded(hub_root, monkeypatch):
    """调用方就地改条目（ghost_memory_candidates 补 suggestion）不许写脏缓存。"""
    _seed_records(hub_root)
    first = brain.similar_records(hub_root, 0.55, 20)
    assert first
    first[0]["verdict"] = "污染"
    first[0]["suggestion"] = "污染"
    again = brain.similar_records(hub_root, 0.55, 20)
    assert again[0]["verdict"] != "污染" and "suggestion" not in again[0], again[0]

    monkeypatch.setattr(brain, "_REC_CACHE_MAX", 2)
    for days in (5, 6, 7):
        brain.similar_records(hub_root, 0.55, 20, days=days)
    assert len(brain._REC_CACHE) == 2
