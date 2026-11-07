# -*- coding: utf-8 -*-
"""R948（B116）：时间感知探针的纯函数守卫（不改生产代码，只钉量具口径）。"""


def _tool():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    if str(root / "tools") not in sys.path:
        sys.path.insert(0, str(root / "tools"))
    import time_aware_probe_r948 as tool
    return tool


def test_window_hit_inclusive_bounds_and_bad_input():
    t = _tool()
    assert t.window_hit("2026-11-03T09:00:00", "2026-11-01", "2026-11-07") is True
    assert t.window_hit("2026-11-01", "2026-11-01", "2026-11-07") is True   # 下界含
    assert t.window_hit("2026-11-07", "2026-11-01", "2026-11-07") is True   # 上界含
    assert t.window_hit("2026-10-31", "2026-11-01", "2026-11-07") is False
    assert t.window_hit("", "2026-11-01", "2026-11-07") is None
    assert t.window_hit("坏日期", "2026-11-01", "2026-11-07") is None


def test_ignored_ratio_counts_only_comparable_rows():
    t = _tool()
    rows = [
        {"window": ("2026-11-01", "2026-11-07"), "timed_ids": [1, 2], "plain_ids": [2, 1]},
        {"window": ("2026-11-01", "2026-11-07"), "timed_ids": [3], "plain_ids": [9]},
        {"window": None, "timed_ids": [1], "plain_ids": [1]},          # 无窗口，不计
        {"window": ("2026-11-01", "2026-11-07"), "error": "boom",
         "timed_ids": [], "plain_ids": []},                              # 出错，不计
    ]
    same, comparable, ratio = t.ignored_ratio(rows)
    assert comparable == 2, (same, comparable)
    assert same == 1, "集合相同但顺序不同仍算相同"
    assert abs(ratio - 0.5) < 1e-9, ratio
    assert t.ignored_ratio([])[2] is None, "零可比较样本不许报 0%"


def test_pairs_are_well_formed_and_time_word_differs():
    t = _tool()
    assert len(t.PAIRS) >= 6
    for timed, plain in t.PAIRS:
        assert timed and plain and timed != plain
        assert plain in timed or len(plain) < len(timed), (timed, plain)
