# -*- coding: utf-8 -*-
"""R1092：配对显著性检验的单测（McNemar 精确二项 + paired bootstrap）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import _ab_common as ab  # noqa: E402


def test_mcnemar_no_discordant_pairs():
    assert ab.mcnemar_exact(0, 0) == 1.0


def test_mcnemar_exact_known_values():
    # g=4,l=0: 2 * C(4,0)/2^4 = 0.125
    assert abs(ab.mcnemar_exact(4, 0) - 0.125) < 1e-6
    # g=14,l=6: 精确二项双尾 ≈ 0.1153（卡方近似给 0.07，精确更保守）
    assert abs(ab.mcnemar_exact(14, 6) - 0.115318) < 1e-4
    assert ab.mcnemar_exact(14, 6) == ab.mcnemar_exact(6, 14)


def test_mcnemar_more_discordant_is_more_significant():
    assert ab.mcnemar_exact(20, 0) < ab.mcnemar_exact(4, 0)


def test_bootstrap_is_deterministic_and_bounded():
    d = [1] * 4 + [0] * 16 + [-1] * 2
    assert ab.paired_bootstrap(d) == ab.paired_bootstrap(d, seed=0)
    lo, hi = ab.paired_bootstrap(d)
    assert lo <= 0 <= hi or lo > 0   # 区间必须能表达「含 0」的可能
    assert lo <= hi


def test_bootstrap_all_positive():
    lo, hi = ab.paired_bootstrap([1] * 20)
    assert lo == hi == 1.0


def test_paired_verdict_counts_and_interval():
    base = [(i, "miss", []) for i in range(1, 11)]
    cand = [(i, "hit" if i <= 4 else "miss", []) for i in range(1, 11)]
    v = ab.paired_verdict(base, cand)
    assert v["gain_n"] == 4 and v["loss_n"] == 0
    assert v["n_pairs"] == 10
    assert abs(v["mcnemar_p"] - 0.125) < 1e-6
    assert v["boot_lo"] > 0     # 4 条净胜在 10 条上区间下界仍 > 0


def test_verdict_with_one_loss_may_include_zero():
    base = [(1, "hit", []), (2, "hit", []), (3, "miss", []), (4, "miss", []),
            (5, "miss", []), (6, "miss", []), (7, "miss", []), (8, "miss", [])]
    cand = [(1, "miss", []), (2, "hit", []), (3, "hit", []), (4, "hit", []),
            (5, "hit", []), (6, "miss", []), (7, "miss", []), (8, "miss", [])]
    v = ab.paired_verdict(base, cand)
    # i=1 hit→miss 是 1 负；i=3/4/5 miss→hit 是 3 胜；i=2/6/7/8 不变。
    assert v["gain_n"] == 3 and v["loss_n"] == 1
    assert v["boot_lo"] <= 0    # 3 胜 1 负、8 条样本：区间含 0，证据不足
    assert v["mcnemar_p"] > 0.05


def test_gate_requires_bootstrap_lower_bound():
    src = (ROOT / "tools" / "_ab_common.py").read_text(encoding="utf-8")
    assert 'pv["boot_lo"] > 0' in src
    assert "McNemar p" in src
