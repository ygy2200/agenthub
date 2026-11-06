# -*- coding: utf-8 -*-
"""R907：不依赖语义路的弱候选兜底（默认关闭）的守卫用例。

钉的是四条护栏，一条都不许松：
  ① 默认 off 且观测字段存在（旧码没有这两个键，本文件在旧码上必红）；
  ② 只在 strong 一条都没有时补位，绝不挤掉任何强命中；
  ③ 最多补 1 条，且按「命中的**不同**检索词数」挑，不是按加权命中分；
  ④ 补位后 rare 计数口径一致、R861 语义桥不抢、常量可一键回滚。

夹具隔离：抬高 `_recall_rare_threshold` 让候选全落进 weak，模拟 R906 真库里
「答案全被稀有词闸门删光」的形状；关掉语义路/桥，避免可选能力改形状（R906 首跑
2 红的教训）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import brain  # noqa: E402


PROMPT = "zetaquux 部署 校验 流程 怎么改"


def _recall(hub_root, tmp_path, monkeypatch, min_hits=0, force_weak=True):
    brain.init_db(str(hub_root))
    terms = [t for t, _w in brain.extract_prompt_terms(PROMPT)]
    assert len(terms) >= 4, terms
    four, two = terms[:4], terms[:2]
    rows = [
        {"id": 1, "content": " ".join(four) + " 目标候选", "tags": "",
         "kind": "lesson", "status": "active"},
        {"id": 2, "content": " ".join(two) + " 干扰甲", "tags": "",
         "kind": "lesson", "status": "active"},
        {"id": 3, "content": " ".join(two) + " 干扰乙", "tags": "",
         "kind": "lesson", "status": "active"},
    ]
    monkeypatch.setattr(brain, "search_memories", lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_semantic_rescue", lambda *a, **k: [])
    if force_weak:
        monkeypatch.setattr(brain, "_recall_rare_threshold", lambda n: 999.0)
    brain.RECALL_WEAK_PICK_MIN = min_hits
    try:
        return brain.prompt_recall(
            str(hub_root), PROMPT, agent="workbuddy", session="unit",
            state_path=str(tmp_path / "state.json"),
            update_state=False, origin="eval")
    finally:
        brain.RECALL_WEAK_PICK_MIN = 0


def test_default_off_has_observable_fields(hub_root, tmp_path, monkeypatch):
    """默认关闭：不补位，但观测字段必须在（A/B 量具据此计精准率）。"""
    out = _recall(hub_root, tmp_path, monkeypatch, min_hits=0)
    assert out["weak_picked"] == 0, out
    assert out["weak_picked_id"] == 0, out
    assert 1 not in out["memory_ids"], out["memory_ids"]


def test_picks_the_most_distinct_weak_candidate(hub_root, tmp_path, monkeypatch):
    """strong 全空时，挑命中不同检索词最多的那条（4 词 vs 2 词）。"""
    out = _recall(hub_root, tmp_path, monkeypatch, min_hits=4)
    assert out["weak_picked"] == 1, out
    assert out["weak_picked_id"] == 1, out
    assert 1 in out["memory_ids"], out["memory_ids"]


def test_never_displaces_strong_candidates(hub_root, tmp_path, monkeypatch):
    """有强命中时一律不补位——护栏 ①，防止把弱摘要塞进本来干净的注入。"""
    out = _recall(hub_root, tmp_path, monkeypatch, min_hits=2, force_weak=False)
    assert out["weak_picked"] == 0, out
    assert out["weak_picked_id"] == 0, out


def test_at_most_one_candidate(hub_root, tmp_path, monkeypatch):
    """多条都够阈值时只补 1 条，不把注入撑满 quota。"""
    out = _recall(hub_root, tmp_path, monkeypatch, min_hits=2)
    assert out["weak_picked"] == 1, out
    assert len([i for i in out["memory_ids"] if i in (1, 2, 3)]) == 1, out["memory_ids"]


def test_threshold_counts_distinct_terms_not_weighted_sum(hub_root, tmp_path, monkeypatch):
    """阈值数的是不同词数，不是 `_recall_hit_score` 的权重和。

    把 hit_score 伪造成 9（远超 need=2），但候选仍只命中 1 个词；min=2 时不该补。
    `_recall_hit_score` 是 sum(权重)，一个高权重大词就能顶出高分，拿它当「共现
    证据」会放进来一堆只碰到一个泛词的元记忆（R830 免疫账的同族问题）。
    """
    out = _recall(hub_root, tmp_path, monkeypatch, min_hits=4)
    # 先确认基线：4 词那条会被补
    assert out["weak_picked"] == 1, out
    terms = [t for t, _w in brain.extract_prompt_terms(PROMPT)]
    rows = [{"id": 1, "content": terms[0] + " 只碰到一个词", "tags": "",
             "kind": "lesson", "status": "active"}]
    monkeypatch.setattr(brain, "search_memories", lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(brain, "_recall_hit_score", lambda *a, **k: 9)
    brain.RECALL_WEAK_PICK_MIN = 2
    try:
        out2 = brain.prompt_recall(
            str(hub_root), PROMPT, agent="workbuddy", session="unit2",
            state_path=str(tmp_path / "state2.json"),
            update_state=False, origin="eval")
    finally:
        brain.RECALL_WEAK_PICK_MIN = 0
    assert out2["weak_picked"] == 0, out2


def test_bookkeeping_and_bridge_after_pick(hub_root, tmp_path, monkeypatch):
    """补位后 rare 计数减 1、语义桥不抢；口径不能自相矛盾。"""
    out = _recall(hub_root, tmp_path, monkeypatch, min_hits=4)
    assert out["dropped"]["rare"] == 2, out["dropped"]
    assert int(out["semantic_bridge"] or 0) == 0, out
