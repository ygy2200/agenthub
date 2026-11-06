# -*- coding: utf-8 -*-
"""R912：gap 报表不许再说「连候选都没有」，要给出逐答案归因。

R904 的措辞是「该路连候选都没有，属召回上限问题，调排序权重无效」。R906 用
逐阶段诊断实锤这是错的：三条 gap 的 4 个期望答案全在 40 条候选池里、全过 hit
闸门、全被稀有词闸门删（rare_gate）。错误结论会把下一轮引向扩召回/调权重，
而真问题在闸门。旧码没有 gap_stages/逐答案归因，本文件在旧码上必红。
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import brain  # noqa: E402
import recall_eval as ev  # noqa: E402


def _empty_recall(root, prompt, **kw):
    """期望路零 top3 → 该条判 gap。"""
    return {"memory_ids": [], "record_ids": [], "terms": ["t"],
            "dropped": {"hit": 0, "rare": 0, "immune": 0, "cap": 0},
            "sem_scored": 0}


def _run(hub_root, cases, recall_fn=None):
    brain.init_db(str(hub_root))
    buf = io.StringIO()
    rc = ev.run(root=str(hub_root), cases=cases,
                recall_fn=recall_fn or _empty_recall, out_stream=buf)
    return rc, buf.getvalue()


def test_report_drops_the_wrong_claim_and_shows_attribution(hub_root, monkeypatch):
    """旧措辞必须消失；报表要给出 rare_gate 这类可行动阶段。"""
    import gap_diagnose_r906 as gd
    monkeypatch.setattr(gd, "diagnose_one", lambda *a, **k: {
        "answers": [{"stage": "rare_gate"}, {"stage": "rare_gate"}]})
    _rc, text = _run(hub_root, [("一条 gap 样本", ("mem", 1), "其他：单条")])
    assert "连候选都没有" not in text, text
    assert "属召回上限问题" not in text, text
    assert "逐答案归因" in text, text
    assert "rare_gate 2" in text, text
    assert "归因：rare_gate 2" in text, text


def test_stage_counts_aggregate_across_gap_samples(hub_root, monkeypatch):
    """两条 gap 的阶段要累加，不是各报各的。"""
    import gap_diagnose_r906 as gd
    monkeypatch.setattr(gd, "diagnose_one", lambda *a, **k: {
        "answers": [{"stage": "rare_gate"}, {"stage": "not_in_pool"}]})
    _rc, text = _run(hub_root, [("样本甲", ("mem", 1), "其他：甲"),
                                ("样本乙", ("mem", 2), "其他：乙")])
    assert "rare_gate 2" in text, text
    assert "not_in_pool 2" in text, text


def test_diagnosis_failure_is_fail_soft(hub_root, monkeypatch):
    """可选诊断炸了不许把整份评测报表带崩。"""
    import gap_diagnose_r906 as gd

    def boom(*a, **k):
        raise RuntimeError("diagnosis unavailable")

    monkeypatch.setattr(gd, "diagnose_one", boom)
    _rc, text = _run(hub_root, [("样本甲", ("mem", 1), "其他：甲")])
    assert "逐答案归因：unknown 1" in text, text
    assert "Recall@3" in text, text


def test_only_gap_samples_trigger_diagnosis(hub_root, monkeypatch):
    """非 gap 样本不跑诊断，省掉几十次重复检索。"""
    import gap_diagnose_r906 as gd
    calls = []

    def spy(*a, **k):
        calls.append(1)
        return {"answers": [{"stage": "rare_gate"}]}

    monkeypatch.setattr(gd, "diagnose_one", spy)

    def mixed(root, prompt, **kw):
        if prompt == "命中的样本":
            return {"memory_ids": [7], "record_ids": [], "terms": ["t"],
                    "dropped": {"hit": 0, "rare": 0, "immune": 0, "cap": 0},
                    "sem_scored": 0}
        return _empty_recall(root, prompt, **kw)

    _rc, _text = _run(hub_root, [("命中的样本", ("mem", 7), "其他：命中"),
                                 ("空样本", ("mem", 1), "其他：空")],
                      recall_fn=mixed)
    assert len(calls) == 1, calls
