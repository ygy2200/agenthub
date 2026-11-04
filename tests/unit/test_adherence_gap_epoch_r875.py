# -*- coding: utf-8 -*-
"""R875：断档要分历史与现行——采集口径上线前的旧账不许永远把体检染红。

背景（真库实测）：R874 部署后桌面版补扫首次真实交账，未观测 68→58、断档 37→28，
但剩下 28 条全部早于「首条带 src 的采集」（2026-11-03T11:49:24）——那是 R849 之前的
旧注入，当时判官拿不到 transcript，证据不可回溯。补扫/回填再修也追不回来。
旧实现把它们算成「漏采集」：zcode 只有 11-02 的 3 条旧注入，每次巡检都红「有注入但
没有采集记录」，真正的新缺陷反而被这条永远红的旧账淹没。

修法：
  1. `recall_adherence_stats` 给出 `capture_start`（流水里第一条带 src 的采集时间），
     并把断档里早于它的一批记为 `unobserved_gap_old`（`unobserved_gap` 仍是总数）；
  2. 巡检的送达判红只用「起点之后还有注入」的家；整段在起点前的家写进
     「历史未闭环：...」，不判红；
  3. 会话粒度的 idle 同款：整段早于起点的会话不计漏判，但会写进闭环账。
起点之后仍零采集的家/会话照旧判红——历史豁免不能变成现行缺陷的挡箭牌。
"""
import datetime
import json

import brain
import ops


def _ins(root: str, ts: str, agent: str, session: str, mid: int, used: int = 0,
         decided: str = "") -> None:
    with brain.db_conn(root) as conn:
        conn.execute(
            "INSERT INTO recall_adherence(ts,session,agent,memory_id,project,strength,fp,"
            "used,decided_at,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, session, agent, mid, "", "strong", '["zebra_r875.py"]', used, decided, "live"))


def _at(hours_ago: float) -> str:
    return (datetime.datetime.now() - datetime.timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def _hub(root: str, agents=("qoder", "workbuddy", "zcode")) -> int:
    brain.init_db(root)
    project = "断档分历史-测试-对象问题"
    for a in agents:
        brain.add_record(root, project, a,
                         datetime.date.today().isoformat(), "开工", "对齐进度")
    return brain.add_memory(root, "断档分历史测试记忆：zebra_r875.py 的口径。",
                            kind="lesson", tags="r875", agent="qoder")


def _capture(root: str, agent: str, session: str, hours_ago: float, chars: int = 4200) -> None:
    note = (f"checked=3 matched=1 mem=[1] chars={chars} src=sweep"
            f" sess={session[:16]}")
    ts = _at(hours_ago)
    brain.journal_add(root, agent, "brain_adherence", "answer", note=note)
    # journal_add 用当前时间；把这条流水拨到指定时刻，构造「采集起点」
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE journal SET ts=? WHERE id=(SELECT MAX(id) FROM journal"
                     " WHERE action='brain_adherence')", (ts,))


def test_stats_splits_gap_by_capture_start(hub_root):
    """断档总数不变，但早于首条 src 采集的一批要单独记成历史。"""
    mid = _hub(hub_root)
    _ins(hub_root, _at(50), "qoder", "r875-hist", mid)
    _ins(hub_root, _at(45), "qoder", "r875-hist", mid)
    _capture(hub_root, "qoder", "r875-live", 40)
    _ins(hub_root, _at(30), "qoder", "r875-cur", mid)
    _ins(hub_root, _at(20), "qoder", "r875-cur", mid)
    st = brain.recall_adherence_stats(hub_root, days=7)
    assert st["capture_start"], st
    assert st["unobserved_gap"] == 2, st       # hist 前一条 + cur 前一条
    assert st["unobserved_gap_old"] == 1, st   # 只有 hist 那条早于起点
    assert st["unobserved_tail"] == 2, st


def test_ops_does_not_red_on_purely_historical_host(hub_root):
    """zcode 只有起点前的旧注入：写进「历史未闭环」，不判红；qoder 正常交账。"""
    mid = _hub(hub_root)
    for _ in range(3):
        _ins(hub_root, _at(100), "zcode", "session_old", mid)
    _capture(hub_root, "qoder", "r875-ok", 90)
    for i in range(6):
        _ins(hub_root, _at(80), "qoder", "r875-ok", mid,
             used=1 if i < 4 else 0, decided=_at(79))
    r = ops._adherence_health(hub_root)
    assert r["ok"] is True, r
    assert any("历史未闭环" in d and "zcode" in d for d in r["detail"]), r["detail"]
    assert any("历史，不计漏判" in d for d in r["detail"]), r["detail"]


def test_ops_still_red_when_post_start_host_has_no_capture(hub_root):
    """起点之后还有注入、却零采集：历史豁免不许把它一起放过去。"""
    mid = _hub(hub_root)
    for _ in range(3):
        _ins(hub_root, _at(100), "zcode", "session_old", mid)
    _capture(hub_root, "qoder", "r875-ok", 90)
    for i in range(6):
        _ins(hub_root, _at(80), "qoder", "r875-ok", mid,
             used=1 if i < 4 else 0, decided=_at(79))
    _ins(hub_root, _at(60), "zcode", "session_new", mid)      # 起点后仍零采集
    r = ops._adherence_health(hub_root)
    assert r["ok"] is False, r
    assert "zcode" in r["summary"], r["summary"]
