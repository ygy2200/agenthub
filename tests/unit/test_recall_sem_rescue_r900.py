# -*- coding: utf-8 -*-
"""R900（B099）：两阶段闸门救援的边界。

先说这些用例在防什么：R899 模拟过「把语义权重放大到 40」能把 #02 救回来，但那时
语义几乎接管排序——外部证据里这叫 weakest-link（弱路把强路拖下来）。所以本轮救援
不放大权重，而是给「被稀有词闸门删掉、但还在词法候选池里」的候选开一条**极窄**的
口子：语义排名前 3、相似度高于噪声峰值、最多 1 条、只补位或换掉语义最弱的一条。

每条用例都对应一条会被人「顺手放宽」的边界，旧码（没有救援）下这些断言会红：
`sem_rescued` 字段不存在、append 不会补位、replace 不会换人。
"""

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "tools"))

PROMPT = "麒麟 貔貅 白泽 通用 泛词 怎么修"
# 夹具别用 ASCII 长标识符：_recall_idf 给长度≥5 的 ASCII 词 2.5 的 idf 保底
# （R825 实锤，防 pyinstaller / qfluentwidgets 这类精确词被池内 df 压零），
# 第一版夹具用 alpha/gamma 造「泛词行」，结果每行都过了稀有词线，五条用例全红。
# 换中文词后稀有度完全由池内 df 决定，strong/weak 才能精确分离。
TERMS = [("麒麟", 1.0), ("貔貅", 1.0), ("白泽", 1.0),
         ("通用", 1.0), ("泛词", 1.0)]


def _row(i, text, **kw):
    d = {"id": i, "content": text, "tags": "", "kind": "lesson",
         "project": "", "use_count": 0, "adopt_count": 0, "inject_count": 0,
         "pinned": 0}
    d.update(kw)
    return d


def _pool(monkeypatch, strong_ids=(1,), weak_ids=(2, 3, 4, 5, 6, 7, 8),
          sem=None, extra=()):
    """构造可精确控制 strong/weak 的候选池。

    每条 strong 行各带一个只在自己身上出现的稀有词（麒麟/貔貅/白泽，池内 df=1
    → raw idf 高 → 过稀有词线）；通用/泛词几乎每条都有（df 过半 → raw 压 0 →
    被 rare 删）。weak 行仍命中通用+泛词两个词，所以 hit>=need，属于「闸门删除」
    而不是「没命中」——正是 B099 要处理的那一类。
    """
    import brain
    rows = []
    for n, i in enumerate(strong_ids):
        rare = ("麒麟", "貔貅", "白泽")[n % 3]
        rows.append(_row(i, f"{rare} 通用 泛词 独有结论{i}"))
    for i in weak_ids:
        rows.append(_row(i, f"通用 泛词 泛化记录{i}"))
    rows.extend(extra)
    monkeypatch.setattr(brain, "extract_prompt_terms", lambda *a, **k: list(TERMS))
    monkeypatch.setattr(brain, "search_memories",
                        lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(brain, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_recall_project_block", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_memory_counters", lambda *a, **k: {})
    monkeypatch.setattr(brain, "_semantic_rank",
                        lambda *a, **k: list(sem or []))


def _sem(i, sim):
    return (i, 1.0 / (60 + i), {"id": i, "_semantic_sim": sim})


def _recall(hub_root, tmp_path, session="s1"):
    import brain
    return brain.prompt_recall(
        str(hub_root), PROMPT, agent="workbuddy", session=session,
        state_path=str(tmp_path / "state.json"),
        update_state=False, origin="eval")


def test_corpus_expanded_and_labels_valid():
    """R900 的评测集必须真的扩到 ≥20 条，且标注合法、提问不重复。

    12 条样本下调出来的权重是过拟合；这条钉住分母，防止后人「为了好看」把难样本
    又删回去（R891 审计：标签要先审，聚合分会骗人）。
    """
    import recall_eval as ev
    assert len(ev.CASES) >= 20, len(ev.CASES)
    prompts = [c[0] for c in ev.CASES]
    assert len(set(prompts)) == len(prompts), "提问重复，分母会虚高"
    for prompt, want, why in ev.CASES:
        assert prompt.strip() and why.strip()
        for kind, mid in ev._accepts(want):
            assert kind in ("mem", "rec")
            assert int(mid) > 0
    # 难度类型要真的覆盖多类，不是 20 条同款
    kinds = {"词汇鸿沟", "闸门类", "多答案", "跨项目", "机制自指"}
    hit_kinds = {k for _, _, why in ev.CASES for k in kinds if k in why}
    assert len(hit_kinds) >= 4, hit_kinds


def test_off_is_noop_and_reports_zero(hub_root, tmp_path, monkeypatch):
    """默认关闭时行为等同旧码：不救援、不改丢弃计数、语义分仍只做重排。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, sem=[_sem(5, 0.90), _sem(6, 0.85)])
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_MODE", "off")
    out = _recall(hub_root, tmp_path)
    assert out["sem_rescued"] == 0, out
    assert out["sem_scored"] == 2
    assert out["dropped"]["rare"] == 7, out["dropped"]
    assert out["memory_ids"] == [1], out["memory_ids"]


def test_append_fills_only_empty_slots(hub_root, tmp_path, monkeypatch):
    """append 只在 picked 有空位时补；原强命中一条都不能被挤掉。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, sem=[_sem(5, 0.90), _sem(6, 0.85), _sem(1, 0.20)])
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_MODE", "append")
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_SIM", 0.65)
    out = _recall(hub_root, tmp_path)
    assert out["sem_rescued"] == 1, out
    assert 1 in out["memory_ids"], out["memory_ids"]   # 强命中没被挤掉
    assert 5 in out["memory_ids"], out["memory_ids"]   # 语义 top1 被补进来
    assert out["dropped"]["rare"] == 6, out["dropped"]


def test_append_never_displaces_full_top3(hub_root, tmp_path, monkeypatch):
    """top3 已满时 append 必须零效果——「只补位」的承诺要能被断言钉住。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, strong_ids=(1, 2, 3), weak_ids=(4, 5, 6, 7, 8),
          sem=[_sem(5, 0.99)])
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_MODE", "append")
    out = _recall(hub_root, tmp_path)
    assert out["sem_rescued"] == 0, out
    assert set(out["memory_ids"]).issubset({1, 2, 3}), out["memory_ids"]


def test_replace_requires_semantic_margin(hub_root, tmp_path, monkeypatch):
    """相似度只高一点点时不换人：0.5942 是「无意义句」噪声峰值（记忆 #542）。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, sem=[_sem(5, 0.70), _sem(1, 0.66)])
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_MODE", "replace")
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_SIM", 0.65)
    out = _recall(hub_root, tmp_path)
    assert out["sem_rescued"] == 0, out
    assert out["memory_ids"] == [1], out["memory_ids"]


def test_replace_swaps_only_when_clearly_closer(hub_root, tmp_path, monkeypatch):
    """差距够大才换，且一次只换一条。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, sem=[_sem(5, 0.95), _sem(6, 0.90), _sem(1, 0.20)])
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_MODE", "replace")
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_SIM", 0.65)
    out = _recall(hub_root, tmp_path)
    assert out["sem_rescued"] == 1, out
    assert out["memory_ids"] == [5], out["memory_ids"]
    assert 6 not in out["memory_ids"], "最多救 1 条"


def test_rescue_only_from_semantic_topn(hub_root, tmp_path, monkeypatch):
    """语义路里排第 4 名的高相似候选不许被救——B071 的原始病灶就是 #463 排 23–36 位。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, sem=[_sem(5, 0.95), _sem(6, 0.94), _sem(7, 0.93),
                            _sem(8, 0.92)])
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_MODE", "replace")
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_SIM", 0.65)
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_TOPN", 3)
    out = _recall(hub_root, tmp_path)
    assert 8 not in out["memory_ids"], out["memory_ids"]


def test_rescue_never_restores_hit_or_immune_drops(hub_root, tmp_path, monkeypatch):
    """语义只翻 rare 这一类的案；hit<need / 曝光免疫是结构性判定，不翻。"""
    import brain
    brain.init_db(str(hub_root))
    # id=9 只命中 1 个词（hit<need）；id=10 推过 5 次零采纳（免疫）
    extra = [_row(9, "通用 半截命中"),
             _row(10, "通用 泛词 免疫", inject_count=8, adopt_count=0,
                  last_inject="2026-11-06T09:00:00")]
    _pool(monkeypatch, sem=[_sem(9, 0.99), _sem(10, 0.99), _sem(5, 0.90)],
          extra=extra)
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_MODE", "replace")
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_SIM", 0.65)
    # R1063 起另有「语义准入」独立档（只补空位、默认 0.68），它**有意**放行 hit<need
    # 的语义近邻——那是另一条通道，不是 R900 救援。本用例测的是救援，故把准入档关掉；
    # 准入档自身的边界（含「免疫条永不补入」）见 tests/unit/test_sem_admit_r1063.py。
    monkeypatch.setattr(brain, "RECALL_SEM_ADMIT_MIN", 0.0)
    out = _recall(hub_root, tmp_path)
    assert 9 not in out["memory_ids"], out["memory_ids"]
    assert 10 not in out["memory_ids"], out["memory_ids"]


def test_rescue_fail_open_without_semantics(hub_root, tmp_path, monkeypatch):
    """语义路没预热/抛异常 → 零救援，结果与纯词法一致（可选能力不许改变兜底）。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, sem=[])
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_MODE", "replace")
    off = _recall(hub_root, tmp_path, session="off")
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_MODE", "off")
    base = _recall(hub_root, tmp_path, session="base")
    for key in ("text", "memory_ids", "record_ids", "terms", "chars"):
        assert off[key] == base[key], (key, off[key], base[key])
    assert off["sem_rescued"] == 0
