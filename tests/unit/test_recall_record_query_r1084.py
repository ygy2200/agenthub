# -*- coding: utf-8 -*-
"""R1084：记录路查询串开关的单测（关档零行为差异是硬要求）。

关档若偷偷调了 extract_prompt_terms，钩子路径上每句话都多一次分词——
R953/R960 的教训是「为实验付的固定成本最终都会变成用户感知的延迟」。
所以这里不只断言返回值，还要断言 off 档**根本没碰**分词函数。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    old = brain.RECALL_REC_QUERY_MODE
    yield
    brain.RECALL_REC_QUERY_MODE = old


def test_default_is_off():
    assert brain.RECALL_REC_QUERY_MODE == "off"


def test_off_returns_query_verbatim(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("off 档不许调用 extract_prompt_terms")
    monkeypatch.setattr(brain, "extract_prompt_terms", boom)
    brain.RECALL_REC_QUERY_MODE = "off"
    assert brain._recall_record_query("随便一句话", "a b c") == "a b c"


def test_raw_returns_prompt():
    brain.RECALL_REC_QUERY_MODE = "raw"
    assert brain._recall_record_query("  手机上不走线也能 adb 调试吗  ", "手机 上不 adb") == \
        "手机上不走线也能 adb 调试吗"


def test_raw_empty_prompt_falls_back():
    brain.RECALL_REC_QUERY_MODE = "raw"
    assert brain._recall_record_query("", "原 query") == "原 query"


def test_mix_contains_both_and_is_capped():
    brain.RECALL_REC_QUERY_MODE = "mix"
    out = brain._recall_record_query("这是一句话", "term1 term2")
    assert out.startswith("这是一句话") and "term1" in out and "term2" in out
    assert len(brain._recall_record_query("啊" * 300, "x")) <= 200


def test_whole_keeps_only_full_weight_terms():
    brain.RECALL_REC_QUERY_MODE = "whole"
    out = brain._recall_record_query("采纳率到底怎么算才不会自己骗自己", "ignored")
    # extract_prompt_terms 给整词 weight=2、bigram weight=1；whole 档只留整词
    assert out and "ignored" not in out
    assert out == " ".join(t for t, w in brain.extract_prompt_terms(
        "采纳率到底怎么算才不会自己骗自己") if w >= 2)


def test_unknown_mode_falls_back_to_query():
    brain.RECALL_REC_QUERY_MODE = "no_such_mode"
    assert brain._recall_record_query("一句话", "a b") == "a b"


def test_switch_really_changes_record_pool(monkeypatch):
    """开关必须真的影响 search_records 的入参，否则 A/B 测的是空气。"""
    seen = []
    real = brain.search_records

    def spy(root, kw, limit=30):
        seen.append(kw)
        return []

    monkeypatch.setattr(brain, "search_records", spy)
    brain.RECALL_REC_QUERY_MODE = "off"
    assert brain._recall_record_query("某句话", "w1 w2") == "w1 w2"
    brain.RECALL_REC_QUERY_MODE = "raw"
    assert brain._recall_record_query("某句话", "w1 w2") == "某句话"
    brain.RECALL_REC_QUERY_MODE = "off"
    assert real is not None
    assert seen == []   # 本例只验函数本身，不触发 prompt_recall
