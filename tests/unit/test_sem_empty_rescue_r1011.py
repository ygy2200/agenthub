# -*- coding: utf-8 -*-
"""R1011：词法有候选、strong 全空时的高相似语义兜底。

与 R861 的分界：R861 只在词法**一条候选都没有**时启用；这里处理的是池里有候选、
但全被 hit/rare 闸门判弱——如果语义 top3 里有一条明确高相似，允许它作为答案补位。
默认关；真实收益/噪声由 tools/sem_empty_ab_r1011.py 同库 A/B 决定。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import brain  # noqa: E402


def _row(mid, content="无关候选", **kw):
    row = {"id": mid, "content": content, "tags": "", "kind": "lesson",
           "project": "", "use_count": 0, "adopt_count": 0,
           "inject_count": 0, "last_inject": "", "pinned": 0,
           "_recall_hit": 0, "_recall_best": 0.0}
    row.update(kw)
    return row


def test_关闭时直接空返():
    rows = [_row(1)]
    assert brain._semantic_empty_rescue(rows, {1: 0.99}, set(), {}, 0.0, 3, 2) == []


def test_只收语义前三且过阈值():
    rows = [_row(1), _row(2), _row(3), _row(4)]
    sem = {1: 0.70, 2: 0.80, 3: 0.79, 4: 0.95}
    out = brain._semantic_empty_rescue(rows, sem, set(), {}, 0.78, 3, 2)
    assert [r["id"] for r in out] == [4, 2]   # 1 被阈值挡；3 过线但被 cap=2 切
    assert out[0]["_semantic_bridge"] == 0.95


def test_免疫与本会话已注入一票否决():
    now = "2026-11-08T12:00:00"
    rows = [_row(1), _row(2), _row(3)]
    acct = {1: {"inject_count": 5, "adopt_count": 0, "last_inject": now}}
    out = brain._semantic_empty_rescue(
        rows, {1: 0.99, 2: 0.98, 3: 0.97}, {2}, acct, 0.70, 3, 2)
    assert [r["id"] for r in out] == [3]


def test_端到端_词法全弱且记录为空时才补答(monkeypatch, tmp_path):
    prompt = "麒麟 貔貅 白泽 凤凰 为什么都不行"
    terms = [("麒麟", 1.0), ("貔貅", 1.0), ("白泽", 1.0), ("凤凰", 1.0)]
    # 命中四个检索词 → 进入 scored；池里全是同样命中泛词的候选，raw idf 低于
    # 随池子放大的 rare_line，因此 strong 为空。这样 R861 的「词法零候选」路径
    # 不触发，才能单独验证 R1011。
    weak = _row(99, "麒麟 貔貅 白泽 凤凰 完全无关的候选")
    decoys = [_row(i, "麒麟 貔貅 白泽 凤凰 泛化记录") for i in range(1, 11)]
    monkeypatch.setattr(brain, "extract_prompt_terms", lambda *a, **k: list(terms))
    monkeypatch.setattr(brain, "search_memories",
                        lambda *a, **k: [dict(r) for r in decoys + [weak]])
    monkeypatch.setattr(brain, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_recall_project_block", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_memory_counters", lambda *a, **k: {})
    monkeypatch.setattr(brain, "_memory_dates", lambda *a, **k: {})
    sem_row = _row(99, "麒麟 貔貅 白泽 凤凰 完全无关的候选", _semantic_sim=0.9)
    monkeypatch.setattr(brain, "_semantic_rank",
                        lambda *a, **k: [(99, 0.1, sem_row)])

    # R1068 起另有「weak 语义补位」独立档（默认 0.72、只在 picked 全空时补位），它同样
    # 会在这条真空里动手。本用例测的是 R1011 自己的开关，故把它关掉——否则两条通道
    # 混在一起，off 档到底关掉了谁就说不清。补位档自身的边界见 test_sem_weak_r1068.py。
    monkeypatch.setattr(brain, "RECALL_SEM_WEAK_MIN", 0.0)
    monkeypatch.setattr(brain, "RECALL_SEM_EMPTY_MIN", 0.0)
    off = brain.prompt_recall(
        str(tmp_path), prompt, agent="workbuddy", session="s-off",
        state_path=str(tmp_path / "off.json"), update_state=False, origin="eval")
    assert off["memory_ids"] == []
    assert off["sem_empty_rescued"] == 0

    monkeypatch.setattr(brain, "RECALL_SEM_EMPTY_MIN", 0.80)
    on = brain.prompt_recall(
        str(tmp_path), prompt, agent="workbuddy", session="s-on",
        state_path=str(tmp_path / "on.json"), update_state=False, origin="eval")
    assert on["memory_ids"] == [99], on
    assert on["sem_empty_rescued"] == 1
    assert on["semantic_bridge"] == 1
    assert "语义桥 0.90" in on["text"]
