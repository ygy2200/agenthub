# -*- coding: utf-8 -*-
"""R1063：语义准入档（B035 的「词法硬闸」子类）。

R1010 把语义候选补进了**池**，但池里还有一道 `hit < eff_need` 的硬闸：候选正文必须
命中 ≥need 个检索词才进打分。补进来的行按定义就不含这些词，于是 183 条补入零收益
（R1062 实测）。本档给这类候选一条独立准入：语义够近就补进注入的**空位**。

本文件只钉边界，收益由 tools/semantic_admit_ab_r1063.py 同库 A/B 决定：
  · 默认档 0.68 来自 41 条样本 A/B（+2.5pp、Δ噪声 0、Δ耗时 +1.1ms）；置 0 即逐位回滚。
  · 三条硬边界：只补空位、绝不挤掉词法命中、离线不补。
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


def _patch(monkeypatch, mems, sem_rows):
    terms = [("麒麟", 1.0), ("貔貅", 1.0)]
    monkeypatch.setattr(brain, "extract_prompt_terms", lambda *a, **k: list(terms))
    monkeypatch.setattr(brain, "search_memories", lambda *a, **k: [dict(r) for r in mems])
    monkeypatch.setattr(brain, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_recall_project_block", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_memory_counters", lambda *a, **k: {})
    monkeypatch.setattr(brain, "_memory_dates", lambda *a, **k: {})
    monkeypatch.setattr(brain, "_recall_rare_threshold", lambda *a, **k: -1.0)
    monkeypatch.setattr(brain, "RECALL_REL_MARGIN", 0.0)
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: list(sem_rows))
    return terms


def _recall(tmp_path, tag):
    return brain.prompt_recall(
        str(tmp_path), "麒麟 貔貅 怎么配", agent="workbuddy", session=f"s-{tag}",
        state_path=str(tmp_path / f"{tag}.json"), update_state=False, origin="eval")


def test_默认档为_0_68_且置零即关(monkeypatch, tmp_path):
    assert brain.RECALL_SEM_ADMIT_MIN == 0.68
    lexical = _row(1, "麒麟 貔貅 词法结论")
    blind = _row(99, "只讲语义的近邻结论", sim=0.80)
    _patch(monkeypatch, [lexical, blind], [(99, 0.02, blind)])

    on = _recall(tmp_path, "on")
    assert on["memory_ids"] == [1, 99], on
    assert on["sem_admitted"] == 1

    monkeypatch.setattr(brain, "RECALL_SEM_ADMIT_MIN", 0.0)
    off = _recall(tmp_path, "off")
    assert off["memory_ids"] == [1], off
    assert off["sem_admitted"] == 0


def test_低于准入线不补(monkeypatch, tmp_path):
    lexical = _row(1, "麒麟 貔貅 词法结论")
    blind = _row(99, "只讲语义的近邻结论", sim=0.67)
    _patch(monkeypatch, [lexical, blind], [(99, 0.02, blind)])
    res = _recall(tmp_path, "low")
    assert res["memory_ids"] == [1], res
    assert res["sem_admitted"] == 0
    assert res["dropped"]["hit"] == 1      # 仍按「词面没命中」记账


def test_只补空位_绝不挤掉词法命中(monkeypatch, tmp_path):
    mems = [_row(i, "麒麟 貔貅 词法结论 %d" % i) for i in (1, 2, 3)]
    blind = _row(99, "只讲语义的近邻结论", sim=0.80)
    mems.append(blind)
    _patch(monkeypatch, mems, [(99, 0.02, blind)])
    res = _recall(tmp_path, "full")
    assert sorted(res["memory_ids"]) == [1, 2, 3], res
    assert 99 not in res["memory_ids"], res
    assert res["sem_admitted"] == 0


def test_每次最多补_RECALL_SEM_ADMIT_CAP_条(monkeypatch, tmp_path):
    lexical = _row(1, "麒麟 貔貅 词法结论")
    blind_a = _row(98, "语义近邻甲", sim=0.80)
    blind_b = _row(99, "语义近邻乙", sim=0.79)
    _patch(monkeypatch, [lexical, blind_a, blind_b],
           [(98, 0.02, blind_a), (99, 0.03, blind_b)])
    monkeypatch.setattr(brain, "RECALL_SEM_ADMIT_CAP", 1)
    res = _recall(tmp_path, "cap1")
    assert res["sem_admitted"] == 1
    assert 98 in res["memory_ids"] and 99 not in res["memory_ids"], res


def test_免疫条语义再近也不补(monkeypatch, tmp_path):
    """曝光免疫是「推了没人用」的断路器，语义相似度不是解药——结构判定不许被绕过。"""
    import datetime
    lexical = _row(1, "麒麟 貔貅 词法结论")
    immune = _row(10, "只讲语义的老噪音", sim=0.95,
                  inject_count=8, adopt_count=0,
                  last_inject=datetime.datetime.now().isoformat(timespec="seconds"))
    _patch(monkeypatch, [lexical, immune], [(10, 0.01, immune)])
    res = _recall(tmp_path, "immune")
    assert res["memory_ids"] == [1], res
    assert res["sem_admitted"] == 0
    assert res["muted"] == 1
