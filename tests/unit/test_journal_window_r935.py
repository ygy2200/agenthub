# -*- coding: utf-8 -*-
"""R935 / B108：长窗口口径不许再靠「最近 500 行」冒充整窗口统计。

修之前会怎样失败（真库实量，不是假想）：journal 有 9843 行（2026-09-30 → 11-06），
而 `brain.journal_list` 把 limit 夹在 `min(limit, 500)`——`tools/rescue_origin_report_r901.py`
请求 30 天、传 limit=20000，实际只拿回最近 500 行（约两天）。库里两个救援 action
一共 108 行，能不能被扫到全看它们挤不挤得进那 500 行的尾巴：R933 因此只能在报表上
打一条「⚠ 未覆盖请求窗口」的免责声明。声明不是答案——B104/B107 这些「攒够真实样本
再复评」的靶子，靠这把尺子永远量不出「攒够了没有」。

要求：给一个**按窗口读**的出口（action 先收窄、只取需要的列、行数上限独立且明说），
救援分档报表改用它；只有真没读完时才报 truncated，并把上限数字打出来。
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import brain

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import rescue_origin_report_r901 as report  # noqa: E402


def _ts(offset_days: float = 0) -> str:
    return (datetime.now() - timedelta(days=offset_days)).strftime("%Y-%m-%dT%H:%M:%S")


def _seed(root, rows):
    """rows: [(action, note, ts)]，直插 journal（绕开 _now()，才能造跨天窗口）。"""
    assert brain.init_db(str(root)) == ""
    with sqlite3.connect(str(brain.db_path(str(root)))) as c:
        c.executemany(
            "INSERT INTO journal(ts,agent,action,target,backup,note,via) "
            "VALUES (?,?,?,?,?,?,?)",
            [(ts, "qoder", a, "prompt", "", note, "") for a, note, ts in rows])
        c.commit()


def test_window_reader_beats_the_list_cap(tmp_path):
    """journal_list 的 500 行夹子是量具自身的天花板：窗口读取器必须读全。"""
    rows = [("brain_recall_sem_rescue", "n=1 mode=replace ids=7 origin=eval", _ts(0))
            for _ in range(520)]
    _seed(tmp_path, rows)
    assert len(brain.journal_list(str(tmp_path), limit=20000)) < 520 + 1
    got, capped = brain.journal_window(str(tmp_path), actions=report.ACTIONS, cutoff=_ts(1)[:10])
    assert capped is False, "520 行远没到窗口口径的上限，不该报截断"
    assert len(got) == 520, len(got)


def test_window_reader_filters_action_and_cutoff(tmp_path):
    """按 action 收窄 + 按 cutoff 过滤：两者都要真生效，否则窗口数字还是假的。"""
    _seed(tmp_path, [
        ("brain_recall_sem_rescue", "origin=live", _ts(0)),
        ("brain_recall_semantic_bridge", "origin=live", _ts(0)),
        ("log_work", "别的动作不该进来", _ts(0)),
        ("brain_recall_sem_rescue", "origin=live 但已过窗", _ts(40)),
    ])
    got, capped = brain.journal_window(str(tmp_path), actions=report.ACTIONS, cutoff=_ts(30)[:10])
    assert capped is False
    acts = sorted({r["action"] for r in got})
    assert acts == ["brain_recall_sem_rescue", "brain_recall_semantic_bridge"], acts
    assert len(got) == 2, [dict(r) for r in got]


def test_window_reports_truncation_with_its_own_cap(tmp_path):
    """真截断时要能判出来，并把用的是哪个上限交出去（不是 journal_list 的 500）。"""
    _seed(tmp_path, [("brain_recall_sem_rescue", f"origin=eval i={i}", _ts(0))
                     for i in range(12)])
    got, capped = brain.journal_window(str(tmp_path), actions=report.ACTIONS,
                                       cutoff=_ts(1)[:10], limit=5)
    assert capped is True and len(got) == 5
    assert brain.JOURNAL_WINDOW_CAP >= 5000, \
        "窗口口径的上限必须显著高于 journal_list 的 500 行展示夹子"


def test_rescue_report_counts_the_whole_window(tmp_path):
    """核心反证：520 笔救援行里 3 笔 live 落在最老那段——旧读法看不见，新读法必须看见。"""
    rows = [("brain_recall_sem_rescue", "n=1 mode=replace ids=9 origin=live", _ts(9))
            for _ in range(3)]
    rows += [("brain_recall_sem_rescue", "n=1 mode=replace ids=7 origin=eval", _ts(0))
             for _ in range(520)]
    _seed(tmp_path, rows)
    data = report.collect(str(tmp_path), days=30)
    assert data["counts"]["brain_recall_sem_rescue"]["live"] == 3, dict(data["counts"])
    assert data["covered"] is True, data
    text = report.render(data)
    assert "未覆盖请求窗口" not in text, text


def test_rescue_report_still_warns_when_window_truncated(tmp_path, monkeypatch):
    """截断照旧要说破，但点名的是窗口上限，不再错指 journal_list 的 500 行。"""
    _seed(tmp_path, [("brain_recall_sem_rescue", f"origin=eval i={i}", _ts(0))
                     for i in range(30)])
    monkeypatch.setattr(brain, "JOURNAL_WINDOW_CAP", 10)   # 上限在调用时读，不绑在默认参数里
    data = report.collect(str(tmp_path), days=30)
    assert data["capped"] is True, data
    text = report.render(data)
    assert "截断" in text or "未覆盖" in text, text
    assert "10" in text, text


def test_hint_probe_sees_aged_session(tmp_path):
    """修一防众：hint 会话探针是同一把尺子的兄弟量具，也得按窗口读。

    520 行新格式流水压在前面，第 9 天那笔会话行落在旧读法（最近 500 行）之外——
    旧口径会把它当不存在，探针于是说「无连发、B048 保持关账」，而那句话的样本其实
    只有最近几百行。
    """
    import hint_session_probe_r905 as probe
    rows = [("brain_recall", "session=s-old-age mem=[] origin=live", _ts(9))]
    rows += [("brain_recall", f"session=s-early-{i} mem=[1] origin=live", _ts(0))
             for i in range(520)]
    _seed(tmp_path, rows)
    data = probe.collect(str(tmp_path), days=30)
    assert data["capped"] is False, data
    assert "s-old-age" in data["sessions"], sorted(data["sessions"])[:3]
    assert data["sessions"]["s-old-age"]["hint"] == 1, data["sessions"]["s-old-age"]


def test_hint_probe_refuses_verdict_when_truncated(tmp_path, monkeypatch):
    """截断时不许照样下结论：判定行必须改成「不据此改 B048」。"""
    import hint_session_probe_r905 as probe
    _seed(tmp_path, [("brain_recall", f"session=s{i} mem=[] origin=live", _ts(0))
                     for i in range(30)])
    monkeypatch.setattr(brain, "JOURNAL_WINDOW_CAP", 10)
    text = probe.render(probe.collect(str(tmp_path), days=30))
    assert "不据此" in text, text
    assert "保持关账" not in text, text


def test_real_db_window_is_complete():
    """真库不变量：救援两个 action 在 30 天窗口里必须一次读全，且没撞上限。

    这是 B107「攒够 ≥30 笔开关后 live」能不能量的前提——上限一撞，攒没攒够都读不出来。
    """
    import json
    cfg = Path.home() / ".agenthub" / "config.json"
    if not cfg.exists():
        import pytest
        pytest.skip("本机无 ~/.agenthub/config.json")
    root = str(json.loads(cfg.read_text(encoding="utf-8")).get("root") or "")
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    got, capped = brain.journal_window(root, actions=report.ACTIONS, cutoff=cutoff)
    assert capped is False, "真库救援行数远小于窗口上限，报截断说明读法又退回了"
    with sqlite3.connect(str(brain.db_path(root))) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM journal WHERE action IN (?,?) AND substr(ts,1,10)>=?",
            report.ACTIONS + (cutoff,)).fetchone()[0]
    assert len(got) == n, (len(got), n)
