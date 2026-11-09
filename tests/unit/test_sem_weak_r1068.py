# -*- coding: utf-8 -*-
"""R1068：weak 语义补位档（B035 的「无人接手死角」子类）。

R1067 逐条归因实测：recall_eval 的 5 条 gap 样本**全部**落在同一个死角——
`picked` 空、`scored` 非空、`weak` 非空，
  · R900 replace 要求 picked 非空（没得换就不动）；
  · R861 零词法桥要求 scored 也空（有候选就不算结构性失明）；
  · R907 弱候选兜底按「命中不同词数」而非语义。
于是三档都不接手，答案明明在池里（命中 2–5 个词）却被稀有词线判弱后丢弃。

本档只补这个真空：picked 全空时，按语义分从 weak 里补位。
默认 0.72 来自 41 条样本 A/B（+2.5pp、Δ噪声 0、Δ耗时 +0.2ms）；置 0 即逐位回滚。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import brain  # noqa: E402


def _row(mid, text, sim=0.0, **kw):
    row = {"id": mid, "content": text, "tags": "", "kind": "lesson",
           "project": "", "use_count": 0, "adopt_count": 0,
           "inject_count": 0, "pinned": 0}
    row.update(kw)
    if sim:
        row["_semantic_sim"] = sim
    return row


def _patch(monkeypatch, mems, sem_rows, rare_line=1.0):
    terms = [("模组", 1.0), ("管理器", 1.0)]
    monkeypatch.setattr(brain, "extract_prompt_terms", lambda *a, **k: list(terms))
    monkeypatch.setattr(brain, "search_memories", lambda *a, **k: [dict(r) for r in mems])
    monkeypatch.setattr(brain, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_recall_project_block", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_memory_counters", lambda *a, **k: {})
    monkeypatch.setattr(brain, "_memory_dates", lambda *a, **k: {})
    # 稀有词线设成高不可攀：让所有候选中性化地掉进 weak（本档要测的正是这批）
    monkeypatch.setattr(brain, "_recall_rare_threshold", lambda *a, **k: rare_line)
    monkeypatch.setattr(brain, "RECALL_REL_MARGIN", 0.0)
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: list(sem_rows))
    return terms


def _recall(tmp_path, tag):
    return brain.prompt_recall(
        str(tmp_path), "模组 管理器 怎么搬", agent="workbuddy", session=f"s-{tag}",
        state_path=str(tmp_path / f"{tag}.json"), update_state=False, origin="eval")


def test_默认档为_0_72_且置零即关(monkeypatch, tmp_path):
    assert brain.RECALL_SEM_WEAK_MIN == 0.72
    weak = _row(26, "模组 管理器 结论", sim=0.75)
    _patch(monkeypatch, [weak], [(26, 0.05, weak)])
    on = _recall(tmp_path, "on")
    assert on["memory_ids"] == [26], on
    assert on["sem_weak_picked"] == 1

    monkeypatch.setattr(brain, "RECALL_SEM_WEAK_MIN", 0.0)
    off = _recall(tmp_path, "off")
    assert off["memory_ids"] == [], off
    assert off["sem_weak_picked"] == 0


def test_语义分低于线不补(monkeypatch, tmp_path):
    weak = _row(26, "模组 管理器 结论", sim=0.71)
    _patch(monkeypatch, [weak], [(26, 0.05, weak)])
    res = _recall(tmp_path, "low")
    assert res["memory_ids"] == [], res
    assert res["sem_weak_picked"] == 0


def test_补位按语义分排序且不超过上限(monkeypatch, tmp_path):
    a = _row(31, "模组 管理器 结论甲", sim=0.74)
    b = _row(32, "模组 管理器 结论乙", sim=0.90)
    c = _row(33, "模组 管理器 结论丙", sim=0.80)
    _patch(monkeypatch, [a, b, c],
           [(31, 0.05, a), (32, 0.02, b), (33, 0.03, c)])
    monkeypatch.setattr(brain, "RECALL_SEM_WEAK_CAP", 2)
    res = _recall(tmp_path, "order")
    assert res["memory_ids"] == [32, 33], res
    assert res["sem_weak_picked"] == 2


def test_词法有强命中时不补位_不许挤占(monkeypatch, tmp_path):
    """picked 非空 = 词法已经给出结论，本档必须一动不动。"""
    lexical = _row(1, "模组 管理器 强命中结论", sim=0.10)
    weak = _row(26, "模组 管理器 结论", sim=0.95)
    _patch(monkeypatch, [lexical, weak],
           [(26, 0.05, weak), (1, 0.50, lexical)], rare_line=-1.0)
    # R900 的 replace 档**会**用语义更强的 weak 换掉词法命中的那条（那是它的本职）。
    # 本用例只测 R1068 的补位档，故把 replace 关掉，否则两种行为混在一起分不清。
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_MODE", "off")
    res = _recall(tmp_path, "occupied")
    # 稀有词线放到 -1 → 两条都是「强命中」，picked 必非空；本档此时必须一声不响。
    # 具体挑中谁属词法+语义重排的职责（不归本档管），这里只钉「本档没动」。
    assert res["memory_ids"], res
    assert res["sem_weak_picked"] == 0
