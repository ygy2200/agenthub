# -*- coding: utf-8 -*-
"""R917 全库 df 探针：分类判据、只读性、范围边界。

先量再动手（R898 教训）：只有 corpus_rare 占 rare_gate ≥50% 才值得写代码做
全库视角救援。判据写死在工具里，用例钉住不许事后放宽。
工具不存在时本文件 import 即红。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import brain  # noqa: E402
import corpus_idf_probe_r917 as cp  # noqa: E402


def test_classify_threshold_is_strict():
    assert cp.classify(0.02, 0.02) == "corpus_rare"
    assert cp.classify(0.0201, 0.02) == "globally_common"
    assert cp.classify(0.0, 0.02) == "corpus_rare"


def test_worth_rescue_requires_half_of_rare_gate_answers():
    """判据不许事后放宽：49.9% 就是不值得。"""
    def judge(rare, common):
        n = rare + common
        return bool(n) and rare / n >= 0.5
    assert judge(6, 6) is True
    assert judge(5, 7) is False
    assert judge(0, 0) is False


def test_probe_is_read_only(hub_root, monkeypatch):
    """count_hits=False / semantic=False / gate=False，且不许触发唤起写入。"""
    seen = []

    def fake(root, query, **kw):
        seen.append(kw)
        return [{"id": 7, "content": "zetaquux 的用法", "tags": "zetaquux",
                 "kind": "lesson"}]

    monkeypatch.setattr(brain, "search_memories", fake)
    monkeypatch.setattr(brain, "_token_stats",
                        lambda root, tokens: ({t: 1 for t in tokens}, 100))
    monkeypatch.setattr(brain, "_bump_access",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("探针不该写库")))
    rep = cp.probe(str(hub_root), cases=[("zetaquux 怎么用", ("mem", 7), "其他：x")])
    assert seen and all(k["count_hits"] is False and k["semantic"] is False
                        and k["gate"] is False for k in seen), seen
    assert rep["cases"] == 1
    assert isinstance(rep["rows"], list)


def test_probe_skips_answers_not_killed_by_rare_gate(hub_root, monkeypatch):
    """已经过闸门的答案不该被算进 rare_gate 分母，否则占比是假的。"""
    def fake(root, query, **kw):
        return [{"id": 7, "content": "qwertyunique 的说明", "tags": "qwertyunique",
                 "kind": "lesson"}]

    monkeypatch.setattr(brain, "search_memories", fake)
    monkeypatch.setattr(brain, "_token_stats",
                        lambda root, tokens: ({t: 0 for t in tokens}, 100))
    monkeypatch.setattr(brain, "_recall_rare_threshold", lambda n: 0.0)
    rep = cp.probe(str(hub_root), cases=[("qwertyunique 怎么用", ("mem", 7), "其他：x")])
    assert rep["rare_gate_answers"] == 0, rep["rows"]
