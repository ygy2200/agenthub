# -*- coding: utf-8 -*-
"""R1110 时间窗选择率护栏：零回归 / 生效 / 硬过滤保留 / fail-open 四条契约。

夹具沿用 R953 的形状（37 条无关填充 + 窗内 1 + 窗外 2 + 无日期 3），
因为小池里 idf 会把提问词全算成泛词，注入本来就空——那样验的是闸门不是护栏。
护栏本身用 monkeypatch 固定覆盖率，与「覆盖率怎么算」解耦；
最后一条用例单独验真库上的覆盖率是比例而不是条数。
"""
import datetime as dt
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _mem(i, text="门禁结果怎么查"):
    return {"id": i, "content": text, "tags": "", "kind": "lesson"}


def _patch(monkeypatch, mems, recs=None, dates=None):
    import brain
    monkeypatch.setattr(brain, "search_memories",
                        lambda *a, **k: [dict(r) for r in mems])
    monkeypatch.setattr(brain, "search_records",
                        lambda *a, **k: [dict(r) for r in (recs or [])])
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: [])
    if dates is not None:
        monkeypatch.setattr(brain, "_memory_dates", lambda *a, **k: dict(dates))


def _recall(hub_root, prompt, tmp_path, session="s1"):
    import brain
    return brain.prompt_recall(
        str(hub_root), prompt, agent="workbuddy", cwd=str(ROOT),
        session=session, state_path=str(tmp_path / "state.json"),
        update_state=False, origin="eval")


def _stack(monkeypatch, hub_root):
    """窗内 1 / 窗外 2 / 无日期 3 + 37 条填充。提问用「昨天」，故 1 必须锚在昨天。"""
    import brain
    brain.init_db(str(hub_root))
    today = dt.date.today()
    inside = (today - dt.timedelta(days=1)).isoformat()
    outside = (today - dt.timedelta(days=40)).isoformat()
    filler = [{"id": 200 + i, "content": "无关填充内容", "tags": "", "kind": "lesson"}
              for i in range(37)]
    _patch(monkeypatch, [_mem(1), _mem(2), _mem(3)] + filler,
           dates={1: inside, 2: outside})
    monkeypatch.setattr(brain, "RECALL_TIME_WINDOW", 1)
    return inside


def test_cover_zero_is_bitwise_legacy(hub_root, tmp_path, monkeypatch):
    """MIN_COVER=0 必须与护栏上线前逐位一致：不写 cover/skipped 字段。"""
    import brain
    _stack(monkeypatch, hub_root)
    monkeypatch.setattr(brain, "RECALL_TIME_MIN_COVER", 0.0)
    out = _recall(hub_root, "昨天的门禁结果", tmp_path)
    assert "time_window_skipped" not in out
    assert "time_window_cover" not in out
    assert out["time_window_dropped"] == 1, out


def test_cover_high_skips_filter_and_keeps_outside(hub_root, tmp_path, monkeypatch):
    """覆盖率低于阈值 → 跳过硬过滤：越窗候选不再被删。"""
    import brain
    _stack(monkeypatch, hub_root)
    monkeypatch.setattr(brain, "_time_window_cover", lambda *a, **k: 0.01)
    monkeypatch.setattr(brain, "RECALL_TIME_MIN_COVER", 0.10)
    out = _recall(hub_root, "昨天的门禁结果", tmp_path)
    assert out.get("time_window_skipped") == 1, out
    assert out["time_window_dropped"] == 0, "护栏生效时不许删候选"
    assert 2 in out["memory_ids"], "越窗候选应留在池里：" + str(out["memory_ids"])


def test_cover_low_keeps_hard_filter(hub_root, tmp_path, monkeypatch):
    """覆盖率高于阈值 → 护栏不介入，仍按 R953 硬过滤。"""
    import brain
    _stack(monkeypatch, hub_root)
    monkeypatch.setattr(brain, "_time_window_cover", lambda *a, **k: 0.5)
    monkeypatch.setattr(brain, "RECALL_TIME_MIN_COVER", 0.10)
    out = _recall(hub_root, "昨天的门禁结果", tmp_path)
    assert not out.get("time_window_skipped")
    assert out["time_window_dropped"] == 1, out
    assert 2 not in out["memory_ids"], out["memory_ids"]


def test_cover_failopen_on_bad_root():
    """库打不开 → 返回 1.0（覆盖充分），过滤照旧，绝不抛异常。"""
    import brain
    bad = str(Path(tempfile.mkdtemp(prefix="r1110d_")) / "nope.bin")
    assert brain._time_window_cover(bad, ("2026-10-01", "2026-10-31")) == 1.0
    assert brain._time_window_cover(bad, None) == 1.0


def test_cover_is_ratio_not_count(hub_root):
    """真库上覆盖率是 (0,1] 的比例：窗内 1 条 / 共 2 条 = 0.5。"""
    import brain
    brain.init_db(str(hub_root))
    conn = sqlite3.connect(str(brain.db_path(str(hub_root))))
    try:
        conn.execute("DELETE FROM memories")
        conn.execute("INSERT INTO memories (id,status,created,superseded_by,content)"
                     " VALUES (1,'active','2026-10-05',0,'甲')")
        conn.execute("INSERT INTO memories (id,status,created,superseded_by,content)"
                     " VALUES (2,'active','2026-01-05',0,'乙')")
        conn.commit()
    finally:
        conn.close()
    c = brain._time_window_cover(str(hub_root), ("2026-10-01", "2026-10-31"))
    assert abs(c - 0.5) < 1e-6, c
