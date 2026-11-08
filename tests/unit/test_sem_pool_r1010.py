# -*- coding: utf-8 -*-
"""R1010：语义候选补池的窄口子（B035 的“池外答案”子类）。

R899 只给词法池内候选加语义分；R861 只在词法一条候选都没有时整块走语义桥。
两者之间还剩一类洞：**词法池有候选，但期望答案被 40 条上限截在池外**。
本轮只测补池器本身的边界，真实收益由 tools/semantic_pool_ab_r1010.py 同库 A/B 决定；
默认档必须关闭，未过 A/B 前不影响生产行为。
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


def _sem(mid, sim, text="语义候选"):
    return (mid, 1.0 / (61 + mid), _row(mid, text, sim=sim))


def test_默认关闭时一个都不补():
    pool = [_row(1, "词法候选")]
    got, added = brain._augment_mem_pool(pool, [_sem(9, 0.9)], cap=0, sim_min=0.62)
    assert got is pool
    assert added == 0


def test_只补未见过的_且遵守相似度与上限():
    pool = [_row(1, "词法候选")]
    got, added = brain._augment_mem_pool(
        pool, [_sem(1, 0.99), _sem(2, 0.61), _sem(3, 0.72), _sem(4, 0.80)],
        cap=2, sim_min=0.62)
    assert [r["id"] for r in got] == [1, 3, 4]
    assert added == 2


def test_补池不改变原行对象():
    original = _row(1, "原行")
    pool = [original]
    sem_row = _row(9, "语义行", sim=0.9)
    got, added = brain._augment_mem_pool(
        pool, [(9, 0.1, sem_row)], cap=1, sim_min=0.62)
    assert got[1] is sem_row
    assert got[1]["id"] == 9
    assert added == 1
    assert original == _row(1, "原行")


def test_端到端_开池后池外语义候选可进注入(monkeypatch, tmp_path):
    prompt = "麒麟 貔貅 语义补池"
    terms = [("麒麟", 1.0), ("貔貅", 1.0)]
    lexical = _row(1, "麒麟 貔貅 词法结论")
    sem_only = _row(99, "麒麟 貔貅 语义结论")
    monkeypatch.setattr(brain, "extract_prompt_terms", lambda *a, **k: list(terms))
    monkeypatch.setattr(brain, "search_memories", lambda *a, **k: [dict(lexical)])
    monkeypatch.setattr(brain, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_recall_project_block", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_memory_counters", lambda *a, **k: {})
    monkeypatch.setattr(brain, "_memory_dates", lambda *a, **k: {})
    monkeypatch.setattr(brain, "_recall_rare_threshold", lambda *a, **k: -1.0)
    monkeypatch.setattr(brain, "RECALL_REL_MARGIN", 0.0)
    monkeypatch.setattr(brain, "_semantic_rank",
                        lambda *a, **k: [_sem(99, 0.75, "麒麟 貔貅 语义结论")])

    off = brain.prompt_recall(
        str(tmp_path), prompt, agent="workbuddy", session="s-off",
        state_path=str(tmp_path / "off.json"), update_state=False, origin="eval")
    assert off["memory_ids"] == [1], off
    assert off["sem_pool_added"] == 0

    monkeypatch.setattr(brain, "RECALL_SEM_POOL_LIMIT", 3)
    on = brain.prompt_recall(
        str(tmp_path), prompt, agent="workbuddy", session="s-on",
        state_path=str(tmp_path / "on.json"), update_state=False, origin="eval")
    assert 99 in on["memory_ids"], on
    assert on["sem_pool_added"] == 1
