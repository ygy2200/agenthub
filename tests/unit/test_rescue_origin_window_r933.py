# -*- coding: utf-8 -*-
"""R933：语义救援分档报表不许把「扫到 500 行」说成「看了 30 天」。

R933 验收 B106 时撞上：`rescue_origin_report_r901.py --days 30 --limit 20000`
表头写着「窗口 30 天」，而 `brain.journal_list` 是 `min(limit, 500)`——真库 500 行
只覆盖 11-04 起约 2 天。报表于是把「可得样本」当「全窗口样本」报出去，live=1 这条
还正好落在改生产配置（11:57）之前。口径错了不影响数字，影响的是**结论强度**：
按 30 天读就会说成「线上几乎不触发」，按实际 2 天只能说「样本不足」。

要求：报表自己说破实际覆盖到哪一天、窗口口径用的是哪个上限、以及 live 行的时间
是否晚于生产开关切换（R914）。

R935 口径修订：本文件的夹具原来喂 `journal_list`（500 行展示夹子），窗口读取器上线后
改喂 `brain.journal_window`，返回 `(rows, capped)`；「覆盖与否」不再由最早一行反推，
而由读取器直接说 `capped`。R933 那两条免责声明用例仍成立，只是换了说法。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import rescue_origin_report_r901 as rep  # noqa: E402


def _rows(ts_list, action="brain_recall_sem_rescue", origin="live"):
    return [{"ts": t, "action": action, "note": f"n=1 origin={origin}"} for t in ts_list]


def _patch(monkeypatch, fake, capped=False):
    monkeypatch.setattr(rep.brain, "journal_window", lambda *a, **k: (fake, capped))


def test_collect_reports_actual_earliest_ts(monkeypatch):
    fake = _rows(["2026-11-05T10:00:00", "2026-11-06T09:00:00", "2026-11-04T23:06:01"])
    _patch(monkeypatch, fake)
    data = rep.collect("root", days=30)
    assert data["earliest"] == "2026-11-04T23:06:01", data
    assert data["covered"] is True and data["capped"] is False, data


def test_render_says_window_is_truncated(monkeypatch):
    fake = _rows(["2026-11-04T23:06:01", "2026-11-06T09:00:00"])
    _patch(monkeypatch, fake, capped=True)
    data = rep.collect("root", days=30)
    text = rep.render(data)
    for needle in ("最早", "截断", str(data["cap"])):
        assert needle in text, (needle, text)


def test_render_silent_when_window_fully_covered(monkeypatch):
    fake = _rows(["2026-01-01T00:00:00", "2026-11-06T09:00:00"])
    _patch(monkeypatch, fake)
    data = rep.collect("root", days=30)
    text = rep.render(data)
    assert "截断" not in text, text


def test_render_dates_live_rows_against_prod_switch(monkeypatch):
    """live 行早于生产开关切换时间就不能算「开关生效后的收益」。"""
    fake = _rows(["2026-11-06T10:48:32"])
    _patch(monkeypatch, fake)
    data = rep.collect("root", days=30)
    text = rep.render(data, switch_ts="2026-11-06T11:57:13")
    assert "早于生产开关" in text, text
    assert "10:48" in text, text


def test_render_no_switch_warning_when_after(monkeypatch):
    fake = _rows(["2026-11-06T16:48:32"])
    _patch(monkeypatch, fake)
    data = rep.collect("root", days=30)
    text = rep.render(data, switch_ts="2026-11-06T11:57:13")
    assert "早于生产开关" not in text, text


def test_live_after_switch_counted_separately(monkeypatch):
    fake = _rows(["2026-11-06T10:48:32", "2026-11-06T16:48:32"])
    _patch(monkeypatch, fake)
    data = rep.collect("root", days=30, switch_ts="2026-11-06T11:57:13")
    assert data["live_after_switch"] == 1, data
