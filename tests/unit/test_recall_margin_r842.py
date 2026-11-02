# -*- coding: utf-8 -*-
"""R842 注入配额的相对阈值：同一条提问里，后位候选必须够得上首位才一起上。

背景（真实库 2026-11-02 实测，口径已修正）：搜索账本近 7 天 284 次 prompt_recall 里
258 次 hits=3 看着像"永远顶满"，但那 227 次集中在 10-31 一个会话的重复提问上——
**不是 284 个独立提问**。用 tools/recall_saturation_probe.py 去重回放 60 条真实提问：
改前平均 2.45 条 / ≥3 条占 53% / 零注入 27%。结论：饱和没有账本看起来那么严重，
但后位候选确实不受约束（改后 18% 的提问收紧，首位一条不掉）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brain  # noqa: E402


def _rows(*specs):
    return [(s, {"id": i}) for i, s in enumerate(specs, 1)]


def test_首位无条件保留即使差距悬殊():
    """首位是最相关的，哪怕它比第二位高一个数量级也不能被自己的阈值砍掉。"""
    out = brain._recall_pick_margin(_rows(10.0, 0.5), 3, 0.62)
    assert [r["id"] for r in out] == [1]


def test_后位够得上比例才跟上():
    out = brain._recall_pick_margin(_rows(10.0, 9.0, 3.0), 3, 0.62)
    assert [r["id"] for r in out] == [1, 2], "3.0 < 10*0.62=6.2，应当被收敛掉"


def test_并列首位都保留():
    out = brain._recall_pick_margin(_rows(5.0, 5.0, 5.0), 3, 1.0)
    assert [r["id"] for r in out] == [1, 2, 3]


def test_cap_仍然生效():
    out = brain._recall_pick_margin(_rows(9, 8, 7, 6, 5), 3, 0.62)
    assert len(out) == 3


def test_margin_关闭时就是_top_cap():
    """<=0 是回放对照用的关闭语义，不能被当成"全砍"。"""
    out = brain._recall_pick_margin(_rows(10.0, 0.1, 0.05), 3, 0)
    assert [r["id"] for r in out] == [1, 2, 3]


def test_空输入不炸():
    assert brain._recall_pick_margin([], 3, 0.62) == []


def test_首位分非正时退化为_top_cap():
    """得分全为 0（极端情形）时不按比例算——0 乘任何比例都是 0，会把后位全砍光。"""
    out = brain._recall_pick_margin(_rows(0.0, 0.0), 3, 0.62)
    assert [r["id"] for r in out] == [1, 2]


def test_默认常量在合理区间():
    assert 0 < brain.RECALL_REL_MARGIN <= 1
    assert brain.RECALL_PICK_CAP == 3
    assert brain.RECALL_RECORD_CAP == 2
