# -*- coding: utf-8 -*-
"""R1090：A/B 耗时判据改用 p50 的单测。

R1089 实锤：同一次扫描里十个档的命中/噪声完全一致，Δ均值却在
+4.6~+89.1ms 之间乱跳——被进程预热/GC/首次语义编码的离群点拉偏。
判据必须写清统计量，否则「50ms 闸门」判的是测量噪声不是代码成本。
"""
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import brain  # noqa: E402
import _ab_common as ab  # noqa: E402


def test_median_ignores_single_outlier():
    ms = [10.0, 10.0, 10.0, 10.0, 1000.0]
    assert statistics.median(ms) == 10.0
    assert sum(ms) / len(ms) > 200   # 均值被离群点带飞


def test_one_reports_p50_p90_avg_and_slowest(monkeypatch):
    delays = [0.001, 0.001, 0.001, 0.001, 0.02]
    calls = {"n": 0}

    def fake_recall(root, prompt, **_kw):
        i = calls["n"]
        calls["n"] += 1
        time.sleep(delays[min(i, len(delays) - 1)])
        return {"memory_ids": [], "record_ids": [], "terms": []}

    def fake_judge(root, case, res):
        return "miss", 0, []

    monkeypatch.setattr(brain, "prompt_recall", fake_recall)
    cases = [(f"q{i}", i, "why") for i in range(1, 6)]
    state = Path(tempfile.mkdtemp(prefix="ab_latency_")) / "state.json"
    r = ab._one("fake_root", cases, state, frozenset(), "probe", fake_judge)
    for key in ("avg_ms", "p50_ms", "p90_ms", "slowest"):
        assert key in r, key
    assert r["p50_ms"] <= r["p90_ms"] <= r["avg_ms"] + 100
    assert r["p50_ms"] < r["avg_ms"]          # 离群点只推高均值
    assert len(r["slowest"]) == 3
    assert r["slowest"][0][0] == 5            # 最慢的是第 5 例


def test_verdict_uses_p50_not_avg():
    """判据字段 delta_ms 必须等于 p50 之差，而不是均值之差。"""
    src = (ROOT / "tools" / "_ab_common.py").read_text(encoding="utf-8")
    assert 'dt = round(r["p50_ms"] - base["p50_ms"], 1)' in src
    assert '"delta_avg_ms": dt_avg' in src
    assert "dt_avg <= 50" not in src
