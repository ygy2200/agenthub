# -*- coding: utf-8 -*-
"""R881 · B089：给采纳账记「策略档位」，让 R879 的分档第一次可被验证。

R879 把注入分成 required/supportive 并给了不同的执行口径，但 `recall_adherence`
表里没有档位字段——「标【必须照做】之后模型到底更听不听」只能标「待确认」。
本轮加一列 + 按档出数，把这句待确认变成一个能量的问题。

口径红线（记忆 #546）：分档合计必须等于顶层，否则账面对不上会让人做反动作。
历史行没有档位 → 归 `unknown`，不许冒充 supportive（否则对比失真）。
老库补列幂等（记忆 #33/#323/#3391）：ALTER 走 _migrate_columns，不放 SCHEMA 的
executescript；重复跑不能 duplicate column。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import brain  # noqa: E402


def _db(root):
    return os.path.join(root, "_hub", "brain.db")


def _row(mid, kind, text):
    return {"id": mid, "content": f"R881TOKEN{mid} {text}", "kind": kind,
            "tags": "", "pinned": 0}


def test_老库缺列时自动补上_且重复跑幂等(hub_root):
    brain.init_db(hub_root)
    with sqlite3.connect(_db(hub_root)) as c:
        c.execute("ALTER TABLE recall_adherence DROP COLUMN directive")
        cols = {r[1] for r in c.execute("PRAGMA table_info(recall_adherence)")}
        assert "directive" not in cols
    assert brain.ensure_schema(hub_root) == ""
    assert brain.ensure_schema(hub_root) == ""       # 幂等：第二次不炸
    with sqlite3.connect(_db(hub_root)) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(recall_adherence)")}
        assert "directive" in cols


def test_注入时写入档位_与渲染同源(hub_root):
    brain.init_db(hub_root)
    req = _row(1, "preference", "动手前核对退出码")
    sup = _row(2, "lesson", "普通观察只作背景")
    n = brain._record_adherence(hub_root, "s-r881", "workbuddy", [req, sup],
                                "演示-分档", "strong", "R881TOKEN1 R881TOKEN2",
                                origin="live")
    assert n == 2
    with sqlite3.connect(_db(hub_root)) as c:
        rows = c.execute("SELECT memory_id, COALESCE(directive,'') "
                         "FROM recall_adherence ORDER BY memory_id").fetchall()
    assert rows == [(1, "required"), (2, "supportive")], rows


def test_分档合计等于顶层_且两档各自出数(hub_root):
    brain.init_db(hub_root)
    rows = [_row(1, "preference", "必须照做"), _row(2, "lesson", "只是背景"),
            _row(3, "lesson", "另一条背景")]
    brain._record_adherence(hub_root, "s-r881b", "workbuddy", rows,
                            "演示-分档", "strong", "R881TOKEN1 R881TOKEN2 R881TOKEN3",
                            origin="live")
    st = brain.recall_adherence_stats(hub_root)
    by = st["by_directive"]
    assert set(by) == {"required", "supportive", "unknown"}, by   # 空档预置，报表不消失
    assert by["required"]["injected"] == 1, by
    assert by["supportive"]["injected"] == 2, by
    assert by["unknown"]["injected"] == 0, by
    # 记忆 #546：分档合计必须与顶层同口径，否则会引导反向动作
    assert sum(b["injected"] for b in by.values()) == st["injected"], (by, st["injected"])
    assert sum(b["fingerprintable"] for b in by.values()) == st["fingerprintable"], by
    assert sum(b["used"] for b in by.values()) == st["used"], by


def test_历史空档位归unknown_不冒充supportive(hub_root):
    """采集口径上线前的旧行没有档位，混进 supportive 会把对比做脏。"""
    brain.init_db(hub_root)
    brain._record_adherence(hub_root, "s-r881c", "workbuddy",
                            [_row(1, "preference", "新写入")], "演示-分档", "strong",
                            "R881TOKEN1", origin="live")
    with sqlite3.connect(_db(hub_root)) as c:
        c.execute("INSERT INTO recall_adherence(ts, session, agent, memory_id, project,"
                  " strength, fp, origin, directive) VALUES(?,?,?,?,?,?,?,?,?)",
                  (brain._now(), "s-r881c", "workbuddy", 99, "演示-分档", "strong",
                   '["R881OLD"]', "live", ""))
    st = brain.recall_adherence_stats(hub_root)
    by = st["by_directive"]
    assert by["unknown"]["injected"] == 1, by
    assert by["supportive"]["injected"] == 0, by
    assert sum(b["injected"] for b in by.values()) == st["injected"], by


def test_装置行不进任何分档分母(hub_root):
    """origin<>live 的行先被排除（R853），分档不能把它们捞回来。"""
    brain.init_db(hub_root)
    brain._record_adherence(hub_root, "eval-r881", "workbuddy",
                            [_row(1, "preference", "评测装置")], "演示-分档", "strong",
                            "R881TOKEN1", origin="eval")
    st = brain.recall_adherence_stats(hub_root)
    assert st["device_rows"] == 1
    assert st["injected"] == 0
    assert sum(b["injected"] for b in st["by_directive"].values()) == 0
