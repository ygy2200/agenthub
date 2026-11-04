# -*- coding: utf-8 -*-
"""R873：未观测必须拆成「尾巴」和「断档」，否则结构性上限会被读成产品缺陷。

尾巴 = 同一会话最后一笔注入：证据要等下一句发言，单轮会话（或用户关掉窗口）永远
等不到——任何宿主都消不掉。断档 = 同一会话后面还有发言（有更晚的注入）却没判到，
那才是采集通道漏了，要修。两者混成一个数时，「未观测 93」既可能是一切正常，也可能
是钩子掉了，报表读不出区别。
"""
import json
from datetime import datetime, timedelta

import pytest


def _ts(minutes_ago: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _ins(hub_root: str, session: str, ts: str, fp: str, used: int = 0,
         decided: str = "") -> None:
    import brain
    with brain.db_conn(hub_root) as conn:
        conn.execute("INSERT INTO recall_adherence(ts,session,agent,memory_id,project,"
                     "strength,fp,used,decided_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (ts, session, "qoder", 1, "", "strong", fp, used, decided))


def test_tail_and_gap_are_counted_separately(hub_root):
    import brain
    brain.init_db(hub_root)
    old = _ts(90)
    newer = _ts(60)
    # A：两笔都没判，后一笔是尾巴，前一笔是断档（后面还发过言）
    _ins(hub_root, "A", old, '["x.py"]')
    _ins(hub_root, "A", newer, '["y.py"]')
    # B：单轮会话，只有一笔 → 尾巴
    _ins(hub_root, "B", old, '["z.py"]')
    # C：判过的两笔不进未观测
    _ins(hub_root, "C", old, '["c.py"]', used=1, decided=newer)
    _ins(hub_root, "C", newer, '["d.py"]', decided=newer)
    # D：抽不出指纹 → 判不了，不算未观测
    _ins(hub_root, "D", old, "[]")

    st = brain.recall_adherence_stats(hub_root, days=30)
    assert st["injected"] == 6, st
    assert st["fingerprintable"] == 5, st
    assert st["unjudgeable"] == 1, st
    assert st["unobserved"] == 3, st
    assert st["unobserved_tail"] == 2, st
    assert st["unobserved_gap"] == 1, st
    # 各档 injected 仍然要把「判不了」的行算进去（分母的诚实部分）
    assert st["by_strength"]["strong"]["injected"] == 6, st["by_strength"]
    assert st["by_strength"]["strong"]["fingerprintable"] == 5, st["by_strength"]


def test_same_timestamp_rows_are_all_tail(hub_root):
    """一次发言注入 3 条时 ts 完全相同：它们都是尾巴，不能把其中 2 条误报成断档。"""
    import brain
    brain.init_db(hub_root)
    ts = _ts(10)
    for i in range(3):
        _ins(hub_root, "A", ts, json.dumps([f"f{i}.py"]))
    st = brain.recall_adherence_stats(hub_root, days=30)
    assert (st["unobserved_tail"], st["unobserved_gap"]) == (3, 0), st
