# -*- coding: utf-8 -*-
"""R1012（B077）：origin 从 note 文本升级为结构化列。

要守的三件事：
  ① 新写入的 origin 真落到 journal.origin，且只接受 live/brief/probe/eval；
  ② 历史行没有 origin 列值，报表仍能从 note 解析，unknown 不被倒推；
  ③ 报表把 brief 单列（此前被误算 unknown），结构化列优先于冲突的 note。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import brain  # noqa: E402
import rescue_origin_report_r901 as report  # noqa: E402


def test_journal_origin_column_written_and_read(hub_root):
    brain.init_db(hub_root)
    brain.journal_add(hub_root, "qoder", "brain_recall_sem_rescue", "prompt",
                      note="n=1 origin=probe", origin="probe")
    brain.journal_add(hub_root, "qoder", "brain_recall_sem_rescue", "prompt",
                      note="n=1 origin=brief", origin="brief")
    rows, capped = brain.journal_window(
        hub_root, actions=("brain_recall_sem_rescue",), cutoff="", limit=10)
    assert capped is False
    assert [r["origin"] for r in rows] == ["probe", "brief"], rows


def test_invalid_origin_not_written(hub_root):
    brain.init_db(hub_root)
    brain.journal_add(hub_root, "qoder", "x", note="origin=live", origin="fake")
    rows = brain.journal_list(hub_root, limit=5)
    assert rows[0]["origin"] == ""


def test_report_prefers_structured_origin_and_counts_brief(hub_root):
    brain.init_db(hub_root)
    brain.journal_add(hub_root, "qoder", "brain_recall_sem_rescue", "prompt",
                      note="冲突文本 origin=eval", origin="brief")
    data = report.collect(hub_root, days=30)
    assert data["counts"]["brain_recall_sem_rescue"]["brief"] == 1
    assert data["counts"]["brain_recall_sem_rescue"]["eval"] == 0


def test_origin_parser_fallbacks():
    assert report._origin_of("n=1 origin=probe") == "probe"
    assert report._origin_of("n=1 origin=brief") == "brief"
    assert report._origin_of("", "live") == "live"
    assert report._origin_of("n=1 origin=eval", "probe") == "probe"
    assert report._origin_of("没写来源") == "unknown"
    assert report._origin_of("n=1 origin=weird") == "unknown"


def test_render_explains_unknown_and_production_total(monkeypatch):
    fake = [
        {"ts": "2026-11-04T10:00:00", "action": "brain_recall_semantic_bridge",
         "note": "n=1 ids=7", "origin": ""},
        {"ts": "2026-11-08T10:00:00", "action": "brain_recall_semantic_bridge",
         "note": "n=1 origin=live", "origin": "live"},
        {"ts": "2026-11-08T11:00:00", "action": "brain_recall_sem_rescue",
         "note": "n=1", "origin": "brief"},
    ]
    monkeypatch.setattr(report.brain, "journal_window", lambda *a, **k: (fake, False))
    data = report.collect("root", days=30)
    text = report.render(data)
    assert "生产入口合计 live+brief = 2" in text, text
    assert "unknown 1" in text and "2026-11-04T10:00:00" in text, text
    assert "不按时间倒推" in text, text


def test_旧行无origin列值仍按note解析(hub_root):
    brain.init_db(hub_root)
    with sqlite3.connect(str(brain.db_path(hub_root))) as conn:
        conn.execute("INSERT INTO journal(ts,agent,action,target,backup,note,origin) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (brain._now(), "qoder", "brain_recall_sem_rescue", "prompt", "",
                      "n=1 origin=live", ""))
        conn.commit()
    data = report.collect(hub_root, days=1)
    assert data["counts"]["brain_recall_sem_rescue"]["live"] == 1, data
