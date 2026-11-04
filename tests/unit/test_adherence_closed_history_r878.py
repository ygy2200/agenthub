# -*- coding: utf-8 -*-
"""R878：历史欠账被回填补判之后，巡检不许再挂「历史未闭环」。

R875 给「整段早于采集口径起点」的家开了历史豁免：不判红、只点名。那条豁免是按
**家/会话有没有注入**算的，不看这些行到底判没判。R878 的离线回填把 zcode 的 3 笔
旧注入判完（decided_at 已写、used=0）后，账已经闭环，detail 里却仍写
「历史未闭环：zcode …」——已还的账继续挂在墙上，等于把历史豁免变成永久标签。

修法：历史点名只统计 `pending > 0`（未判定）的会话/家；判过的旧行静默退出。
本文件钉住两个方向，防止修成「历史豁免整体消失」：
  1. 旧行全判过 → 不出现「历史未闭环」；
  2. 旧行还有没判的 → 仍按 R875 点名，但不判红。
"""
import datetime

import brain
import ops


def _at(hours_ago: float) -> str:
    return (datetime.datetime.now() - datetime.timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def _hub(root: str) -> int:
    brain.init_db(root)
    project = "历史闭环-测试-对象问题"
    for a in ("qoder", "zcode"):
        brain.add_record(root, project, a, datetime.date.today().isoformat(),
                         "开工", "对齐进度")
    return brain.add_memory(root, "历史闭环测试记忆：zebra_r878.py 的口径。",
                            kind="lesson", tags="r878", agent="qoder")


def _ins(root: str, ts: str, agent: str, session: str, mid: int,
         used: int = 0, decided: str = "") -> None:
    with brain.db_conn(root) as conn:
        conn.execute(
            "INSERT INTO recall_adherence(ts,session,agent,memory_id,project,strength,fp,"
            "used,decided_at,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, session, agent, mid, "", "strong", '["zebra_r878.py"]', used,
             decided, "live"))


def _capture(root: str, hours_ago: float) -> None:
    brain.journal_add(root, "qoder", "brain_adherence", "answer",
                      note="checked=3 matched=1 mem=[1] chars=4200 src=sweep sess=r878-ok")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE journal SET ts=? WHERE id=(SELECT MAX(id) FROM journal"
                     " WHERE action='brain_adherence')", (_at(hours_ago),))


def test_judged_historical_rows_are_not_named_unclosed(hub_root):
    """zcode 的旧注入已由回填逐条判定：detail 不许再说「历史未闭环」。"""
    mid = _hub(hub_root)
    for _ in range(3):
        _ins(hub_root, _at(100), "zcode", "session_old", mid,
             used=0, decided=_at(99))
    _capture(hub_root, 90)
    for i in range(6):
        _ins(hub_root, _at(80), "qoder", "r878-ok", mid,
             used=1 if i < 4 else 0, decided=_at(79))
    r = ops._adherence_health(hub_root)
    assert r["ok"] is True, r
    assert not any("历史未闭环" in d for d in r["detail"]), r["detail"]
    assert not any("历史，不计漏判" in d for d in r["detail"]), r["detail"]


def test_unjudged_historical_rows_are_still_named(hub_root):
    """只判了一部分：还欠的那条照旧点名（不判红），R875 的豁免不能被修没。"""
    mid = _hub(hub_root)
    _ins(hub_root, _at(100), "zcode", "session_old", mid, used=0, decided=_at(99))
    _ins(hub_root, _at(100), "zcode", "session_old", mid, used=0)   # 未判
    _capture(hub_root, 90)
    for i in range(6):
        _ins(hub_root, _at(80), "qoder", "r878-ok", mid,
             used=1 if i < 4 else 0, decided=_at(79))
    r = ops._adherence_health(hub_root)
    assert r["ok"] is True, r
    assert any("历史未闭环" in d and "zcode" in d for d in r["detail"]), r["detail"]
