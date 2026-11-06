# -*- coding: utf-8 -*-
"""R915 池深探针：分类口径、只读性、异常兜底。

三分类必须写死：already_in（40 就在池）/ pool_cap（扩池可救）/
lexical_mismatch（扩到最大仍不在）。混了口径，R916 就会拿扩池去硬凑词汇鸿沟，
重复 R898 那种「补丁一次不触发」的空转。

工具不存在时本文件 import 即红。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import brain  # noqa: E402
import pool_depth_probe_r915 as pd  # noqa: E402


def test_classify_three_stages():
    L = (40, 100, 200)
    assert pd.classify({40: True, 100: True, 200: True}, L) == "already_in"
    assert pd.classify({40: False, 100: False, 200: True}, L) == "pool_cap"
    assert pd.classify({40: False, 100: False, 200: False}, L) == "lexical_mismatch"


def test_first_limit_is_the_smallest_rescue_depth():
    L = (40, 100, 200)
    assert pd.first_limit({40: True, 100: True, 200: True}, L) == 40
    assert pd.first_limit({40: False, 100: True, 200: True}, L) == 100
    assert pd.first_limit({40: False, 100: False, 200: False}, L) is None


def test_custom_limits_do_not_mutate_global_default():
    """显式传 limits 不许改全局常量，否则一条用例会污染后面的分类。"""
    before = tuple(pd.LIMITS)
    rep = pd.probe("D:/unused-r915", cases=[("假样本", ("mem", 7), "其他：x")],
                   limits=(10, 20))
    assert tuple(pd.LIMITS) == before
    assert rep["limits"] == [10, 20]


def test_probe_uses_read_only_search_parameters(hub_root, monkeypatch):
    """与 prompt_recall 取池同参：count_hits=False / semantic=False / gate=False。"""
    seen = []

    def fake(root, query, **kw):
        seen.append(kw)
        return [{"id": 7}]

    monkeypatch.setattr(brain, "search_memories", fake)
    monkeypatch.setattr(brain, "_bump_access",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("探针不该唤起写入")))
    rep = pd.probe(str(hub_root), cases=[("假样本", ("mem", 7), "其他：x")],
                   limits=(40, 100))
    assert len(seen) == 2, seen
    for kw in seen:
        assert kw["count_hits"] is False and kw["semantic"] is False
        assert kw["gate"] is False, kw
    assert rep["by_stage"]["already_in"] == 1, rep["by_stage"]


def test_single_depth_failure_is_fail_soft(hub_root, monkeypatch):
    """某一档检索炸了不许中断整份探针。"""
    calls = []

    def fake(root, query, **kw):
        calls.append(kw["limit"])
        if kw["limit"] == 100:
            raise RuntimeError("db busy")
        return [{"id": 7}]

    monkeypatch.setattr(brain, "search_memories", fake)
    rep = pd.probe(str(hub_root), cases=[("假样本", ("mem", 7), "其他：x")],
                   limits=(40, 100, 200))
    assert calls == [40, 100, 200], calls
    assert rep["by_stage"]["already_in"] == 1, rep["by_stage"]


def test_render_states_the_decision_rule():
    rep = {"root": "r", "cases": 1, "answers": 1, "limits": [40, 100],
           "by_stage": {"pool_cap": 1}, "by_limit": {"40": 0, "100": 1},
           "per_answer": [{"case": 1, "id": 7, "prompt": "样本",
                           "hits": {40: False, 100: True},
                           "stage": "pool_cap", "first_limit": 100}]}
    text = pd.render(rep)
    assert "pool_cap 1" in text
    assert "R916" in text
    assert "limit=100" in text
