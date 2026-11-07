# -*- coding: utf-8 -*-
"""R947（B113）：默认有效期量具的纯函数守卫。

R942 已核实检索侧过滤正确，本轮**不改生产代码**，只钉住量具自己的口径：
分母为 0 时必须能算出「0 条会退出」而不是抛错或假装没误伤（记忆 #566）。
"""
import datetime as dt


def _tool():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    if str(root / "tools") not in sys.path:
        sys.path.insert(0, str(root / "tools"))
    import valid_until_default_ab_r947 as tool
    return tool


def test_age_days_parses_iso_and_rejects_garbage():
    t = _tool()
    today = dt.date(2026, 11, 7)
    assert t.age_days("2026-11-01T10:00:00", today) == 6
    assert t.age_days("2026-11-07", today) == 0
    assert t.age_days("", today) is None
    assert t.age_days("不是日期", today) is None
    assert t.age_days(None, today) is None


def test_zero_expiry_is_reported_as_zero_not_as_no_harm():
    """库龄不足时四个档位全是 0 条退出——分母为 0，不许报成「通过」。"""
    t = _tool()
    today = dt.date(2026, 11, 7)
    rows = [{"id": i, "kind": "lesson", "created": f"2026-10-{d:02d}",
             "valid_until": "", "last_inject": "2026-11-06", "adopt_count": 1}
            for i, d in enumerate((8, 18, 28), 1)]
    for pol in (90, 180, 365, t.KIND_POLICY):
        s = t.expire_stats(rows, pol, today)
        assert s["expired"] == 0, (pol, s)
        assert s["still_used"] == 0, s
        assert s["harm_rate"] is None, "0 分母不许报成 0% 的好成绩"


def test_old_but_still_used_is_counted_as_harm():
    t = _tool()
    today = dt.date(2026, 11, 7)
    rows = [
        {"id": 1, "kind": "lesson", "created": "2026-01-01", "valid_until": "",
         "last_inject": "2026-11-06", "adopt_count": 1},
        {"id": 2, "kind": "lesson", "created": "2026-01-01", "valid_until": "",
         "last_inject": "", "adopt_count": 0},
    ]
    s = t.expire_stats(rows, 90, today)
    assert s["expired"] == 2, s
    assert s["still_used"] == 1, s
    assert abs(s["harm_rate"] - 0.5) < 1e-9, s


def test_explicit_valid_until_is_exempt_from_default_policy():
    t = _tool()
    today = dt.date(2026, 11, 7)
    rows = [{"id": 1, "kind": "fact", "created": "2026-01-01",
             "valid_until": "2027-04-30", "last_inject": "", "adopt_count": 0}]
    assert t.expire_stats(rows, 90, today)["expired"] == 0


def test_kind_policy_uses_different_horizons():
    """100 天前的 note 该过期、fact 不该（门槛 90 vs 365）。"""
    t = _tool()
    today = dt.date(2026, 11, 7)
    rows = [
        {"id": 1, "kind": "note", "created": "2026-07-30", "valid_until": "",
         "last_inject": "", "adopt_count": 0},
        {"id": 2, "kind": "fact", "created": "2026-07-30", "valid_until": "",
         "last_inject": "", "adopt_count": 0},
    ]
    s = t.expire_stats(rows, t.KIND_POLICY, today)
    assert s["expired"] == 1, s
    assert s["by_kind"] == {"note": 1}, s
