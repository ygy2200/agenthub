# -*- coding: utf-8 -*-
"""R913：注入的记忆要带日期，模型才有办法判断陈旧。

外部依据（buildwithaitoday《Memory Retrieval》）：注入块里写
"[2026-08-14] moved to Denver" 能让模型推理时效性；不带日期的块不能。
本仓记录通道一直带 date，记忆通道只带 #id/kind/命中注脚——三个月前的结论和
今天的结论长得一模一样。旧码渲染里没有日期，本文件在旧码上必红。

只改展示：memory_ids / 采纳账 / 注入预算一律不动，用例钉住这条边界。
夹具把「插入」与「召回」拆开：首版每次都新插一条记忆，第二次召回只命中新
那条，目标 id 根本不在注入里，断言查不到（R906 同族的形状错误）。
"""
import datetime
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import brain  # noqa: E402


def _insert(hub_root):
    brain.init_db(str(hub_root))
    return brain.add_memory(str(hub_root), "zetaquux 的用法说明",
                            kind="lesson", tags="zetaquux")


def _recall(hub_root, tmp_path, session="unit"):
    return brain.prompt_recall(
        str(hub_root), "zetaquux 怎么用", agent="workbuddy", session=session,
        state_path=str(tmp_path / f"state-{session}.json"),
        update_state=False, origin="eval")


def _update(hub_root, mid, sql):
    conn = sqlite3.connect(str(brain.db_path(str(hub_root))))
    conn.execute(sql, (mid,))
    conn.commit()
    conn.close()


def test_injected_memory_shows_its_date(hub_root, tmp_path):
    mid = _insert(hub_root)
    out = _recall(hub_root, tmp_path)
    assert mid in out["memory_ids"], out["memory_ids"]
    today = datetime.date.today().isoformat()
    assert f"[{today}]" in out["text"], out["text"]


def test_updated_wins_over_created(hub_root, tmp_path):
    """改过的结论按最后修改时间算，不按创建时间。"""
    mid = _insert(hub_root)
    _update(hub_root, mid, "UPDATE memories SET created='2020-01-01T00:00:00', "
                           "updated='2026-03-17T09:00:00' WHERE id=?")
    out = _recall(hub_root, tmp_path, "s-updated")
    assert "[2026-03-17]" in out["text"], out["text"]
    assert "[2020-01-01]" not in out["text"], out["text"]
    assert brain._memory_dates(str(hub_root), [mid])[mid] == "2026-03-17"


def test_missing_date_never_breaks_injection(hub_root, tmp_path):
    """日期是注脚，查不到就省略，不许影响召回、也不许凭空编一个。"""
    mid = _insert(hub_root)
    _update(hub_root, mid, "UPDATE memories SET created='', updated='' WHERE id=?")
    out = _recall(hub_root, tmp_path, "s-nodate")
    assert mid in out["memory_ids"], out["memory_ids"]
    assert "zetaquux" in out["text"], out["text"]
    line = next(l for l in out["text"].splitlines() if f"#{mid} " in l)
    assert not re.search(r"\[\d{4}-\d{2}-\d{2}\]", line), line
    assert brain._memory_dates(str(hub_root), [mid]) == {}


def test_date_change_does_not_touch_ids_or_budget(hub_root, tmp_path):
    """只改展示：memory_ids 与注入预算字段不受影响。"""
    mid = _insert(hub_root)
    out = _recall(hub_root, tmp_path, "s-ids")
    assert out["memory_ids"] == [mid], out["memory_ids"]
    assert out["skipped"] == "", out["skipped"]
    assert out["directive_required"] == 0, out["directive_required"]


def test_memory_dates_handles_empty_ids():
    assert brain._memory_dates("D:/nonexistent-root-r913", []) == {}
