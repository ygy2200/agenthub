# -*- coding: utf-8 -*-
"""R1098：_parse_time_range 补 ISO 绝对日期分支（R1097 定向验证发现的真缺陷）。

旧实现支持「最近三天 / 昨天 / 上周 / 10月25号」，却对最明确的
`2026-10-25` 返回 None —— 引用历史记录时写标准日期很常见，等于把
最强的时间信号丢掉。修法放在解析最前，非法日期 fail-open 返回 None。
"""
import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402


def test_iso_hyphen():
    assert brain._parse_time_range("2026-10-25 那次调研结论是什么") == \
        ("2026-10-25", "2026-10-25")


def test_iso_dot_and_slash():
    assert brain._parse_time_range("2026.10.25 的记录") == ("2026-10-25", "2026-10-25")
    assert brain._parse_time_range("查 2026/10/25 日志") == ("2026-10-25", "2026-10-25")


def test_iso_single_digit_month_day():
    assert brain._parse_time_range("2026-1-5 那次") == ("2026-01-05", "2026-01-05")


def test_invalid_date_fails_open():
    # 13 月 / 2 月 30 日都不该返回错误区间；交后续中文分支或 None。
    assert brain._parse_time_range("2026-13-45 那次") is None
    assert brain._parse_time_range("2026-02-30 那次") is None


def test_iso_does_not_match_inside_long_number():
    # 时间戳/长数字串里的片段不许被当成日期
    assert brain._parse_time_range("12026-10-251 这段") is None


def test_chinese_relative_regression():
    # R949/R953 的既有行为不许被新分支带偏
    today = datetime.date.today()
    assert brain._parse_time_range("昨天改的 brain.py") == (
        (today - datetime.timedelta(days=1)).isoformat(),
        (today - datetime.timedelta(days=1)).isoformat())
    assert brain._parse_time_range("最近三天有没有新样本") == (
        (today - datetime.timedelta(days=3)).isoformat(), today.isoformat())
    assert brain._parse_time_range("10月25号那次调研") == (
        f"{today.year}-10-01", f"{today.year}-10-31")


def test_no_time_word_returns_none():
    assert brain._parse_time_range("这个坑到底怎么修的") is None


def test_prompt_recall_time_window_lit(monkeypatch):
    """生产形状：带 ISO 日期的提问应点亮 time_window 且真的过滤候选。"""
    import tempfile
    from pathlib import Path as P
    sys.path.insert(0, str(ROOT / "tools"))
    import recall_eval as ev  # noqa: E402  tools/ 在 sys.path 补好后才能导入
    root = ev._brain_root()
    state = P(tempfile.mkdtemp(prefix="iso_probe_")) / "state.json"
    r = brain.prompt_recall(root, "2026-10-25 那次调研结论是什么",
                            agent="workbuddy", cwd=str(ROOT),
                            session="iso-unit", state_path=str(state),
                            update_state=False, origin="probe")
    assert r.get("time_window") == ["2026-10-25", "2026-10-25"]
    assert int(r.get("time_window_dropped") or 0) >= 0
