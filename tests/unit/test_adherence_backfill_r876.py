# -*- coding: utf-8 -*-
"""R876：会话关闭后的离线逐轮回填。

病根：补扫靠 UserPromptSubmit 触发；会话一关就再也没人叫它，R874 之前那些
「注入后没等来下一句」的账永远悬在未观测里——transcript 其实还在磁盘上。
回填必须按轮切区间：只用下界会拿后一轮的产出判前一轮的注入（R851 假信号的反向形态）。

修法：
  1. `mark_recall_used(not_after=)`：判定区间 [not_before, not_after)；
  2. `tools/adherence_backfill.py`：找 transcript（qoder/claude/workbuddy 三家）→
     切轮 → 逐轮判定；默认 dry-run，`--apply` 才写账，流水 src=backfill；
  3. 补扫兜底目录补上 workbuddy（R874 只搜了前两家）。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import brain

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import adherence_backfill as bf  # noqa: E402


def _ins(root: str, ts: str, session: str, mid: int, fp: list, agent: str = "qoder") -> int:
    with brain.db_conn(root) as conn:
        cur = conn.execute(
            "INSERT INTO recall_adherence(ts,session,agent,memory_id,project,strength,fp,"
            "used,decided_at,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, session, agent, mid, "", "strong", json.dumps(fp), 0, "", "live"))
        return int(cur.lastrowid)


def _local(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _utc_z(dt_local: datetime) -> str:
    return dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _ms(dt_local: datetime) -> int:
    return int(dt_local.timestamp() * 1000)


def _write_qoder(path: Path, events: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                    encoding="utf-8")


def _q(role: str, dt_local: datetime, blocks: list) -> dict:
    return {"type": role, "timestamp": _utc_z(dt_local),
            "message": {"role": role, "content": blocks}}


def _rows(root: str, session: str) -> list:
    with brain.db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, ts, used, decided_at FROM recall_adherence WHERE session=?"
            " ORDER BY ts", (session,))]


def test_not_after_bounds_mark_recall_used(hub_root):
    """区间是 [not_before, not_after)：区间外的行一个都不许动。"""
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "区间测试记忆。", kind="lesson", tags="r876")
    now = datetime.now()
    ts = [_local(now - timedelta(hours=h)) for h in (3, 2, 1)]
    ids = [_ins(hub_root, t, "r876-window", mid, ["zebra_r876.py"]) for t in ts]
    out = brain.mark_recall_used(
        hub_root, "r876-window", "完全无关的文本", window_h=24,
        not_before=_local(now - timedelta(hours=2, minutes=30)),
        not_after=_local(now - timedelta(hours=1, minutes=30)))
    assert out["checked"] == 1, out
    st = {r["id"]: (r["used"], bool(r["decided_at"])) for r in _rows(hub_root, "r876-window")}
    assert st[ids[1]] == (0, True), st        # 区间内：判过（没命中）
    assert st[ids[0]] == (0, False), st       # 早于下界：没判
    assert st[ids[2]] == (0, False), st       # 晚于上界：没判


def test_backfill_two_turns_does_not_cross_credit(hub_root, tmp_path):
    """第二轮才出现的关键词，不许替第一轮的注入领功。"""
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "回填测试记忆。", kind="lesson", tags="r876")
    session = "r876-two-turns"
    t0 = datetime.now() - timedelta(hours=2)
    t1 = datetime.now() - timedelta(hours=1)
    projects = tmp_path / "projects"
    _write_qoder(projects / "slug-a" / f"{session}.jsonl", [
        _q("user", t0, [{"type": "text", "text": "第一问"}]),
        _q("assistant", t0 + timedelta(seconds=10),
           [{"type": "text", "text": "改了 alpha876.py"}]),
        _q("user", t1, [{"type": "text", "text": "第二问"}]),
        _q("assistant", t1 + timedelta(seconds=10),
           [{"type": "tool_use", "name": "Bash",
             "input": {"command": "python beta876.py"}}]),
    ])
    row_a = _ins(hub_root, _local(t0 + timedelta(seconds=5)), session, mid, ["alpha876.py"])
    row_b = _ins(hub_root, _local(t0 + timedelta(seconds=6)), session, mid, ["beta876.py"])
    row_c = _ins(hub_root, _local(t1 + timedelta(seconds=5)), session, mid, ["beta876.py"])
    out = bf.run(hub_root, [{"session": session, "agent": "qoder"}],
                 [projects], apply=True, days=7, min_idle_h=0)
    assert out["sessions"][0]["turns"] == 2, out
    st = {r["id"]: (r["used"], bool(r["decided_at"])) for r in _rows(hub_root, session)}
    assert st[row_a] == (1, True), st          # 第一轮命中
    assert st[row_b] == (0, True), st          # 第一轮判过：无 beta；第二轮不许回判
    assert st[row_c] == (1, True), st          # 第二轮命中
    # 第二轮的证据不能替 row_b 领功——它只属于第一轮区间（下界 t1 之外，不进判定）
    assert brain.mark_recall_used(hub_root, session, "beta876.py", window_h=24,
                                  not_before=_local(t1))["checked"] == 0, \
        "row_b 在第二轮区间之外，不该被第二轮再判一次"


def test_workbuddy_notification_is_not_a_turn(hub_root, tmp_path):
    """task-notification 不算真人开口；function_call 参数与 output_text 都算证据。"""
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "workbuddy 回填测试。", kind="lesson", tags="r876")
    session = "r876-workbuddy"
    t0 = datetime.now() - timedelta(hours=3)
    events = [
        {"type": "message", "role": "user", "timestamp": _ms(t0),
         "content": [{"type": "input_text",
                      "text": "<system-reminder>上下文</system-reminder>\n继续"}]},
        {"type": "function_call", "timestamp": _ms(t0 + timedelta(seconds=5)),
         "name": "Bash", "arguments": {"command": "cat gamma876.py"}},
        {"type": "message", "role": "user",
         "timestamp": _ms(t0 + timedelta(seconds=30)),
         "content": [{"type": "input_text",
                      "text": "<task-notification>后台完成</task-notification>"}]},
        {"type": "message", "role": "assistant",
         "timestamp": _ms(t0 + timedelta(seconds=40)),
         "content": [{"type": "output_text", "text": "看了 gamma876.py，没问题"}]},
    ]
    projects = tmp_path / "projects"
    _write_qoder(projects / "slug-w" / f"{session}.jsonl", events)
    row = _ins(hub_root, _local(t0 + timedelta(seconds=1)), session, mid,
               ["gamma876.py"], agent="workbuddy")
    turns = bf.turns_from_events(events)
    assert len(turns) == 1, turns
    assert "gamma876.py" in "\n".join(turns[0]["evidence"])
    out = bf.run(hub_root, [{"session": session, "agent": "workbuddy"}],
                 [projects], apply=True, days=7, min_idle_h=0)
    assert out["sessions"][0]["turns"] == 1, out
    st = {r["id"]: (r["used"], bool(r["decided_at"])) for r in _rows(hub_root, session)}
    assert st[row] == (1, True), st


def test_missing_transcript_is_honest(hub_root, tmp_path):
    """找不到 transcript 就报 no-file，不改账。"""
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "缺文件测试。", kind="lesson", tags="r876")
    session = "r876-no-file"
    row = _ins(hub_root, _local(datetime.now() - timedelta(hours=2)), session, mid,
               ["zebra_r876.py"])
    projects = tmp_path / "projects"
    projects.mkdir()
    out = bf.run(hub_root, [{"session": session, "agent": "qoder"}],
                 [projects], apply=True, days=7, min_idle_h=0)
    assert out["sessions"][0]["status"] == "no-file", out
    st = {r["id"]: (r["used"], bool(r["decided_at"])) for r in _rows(hub_root, session)}
    assert st[row] == (0, False), st


def test_active_session_is_not_judged_early(hub_root, tmp_path):
    """最后注入还很新（10 分钟前）：回填不许抢在线补扫的活，提前判会误伤。"""
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "活跃会话保护测试。", kind="lesson", tags="r876")
    session = "r876-active"
    t0 = datetime.now() - timedelta(minutes=10)
    projects = tmp_path / "projects"
    _write_qoder(projects / "slug-a" / f"{session}.jsonl", [
        _q("user", t0, [{"type": "text", "text": "刚问的"}]),
        _q("assistant", t0 + timedelta(seconds=5),
           [{"type": "text", "text": "zebra_r876.py 改完了"}]),
    ])
    row = _ins(hub_root, _local(t0 + timedelta(seconds=1)), session, mid,
               ["zebra_r876.py"])
    out = bf.run(hub_root, [{"session": session, "agent": "qoder",
                             "last_ts": _local(t0)}],
                 [projects], apply=True, days=7)
    assert out["sessions"][0]["status"] == "active", out
    st = {r["id"]: (r["used"], bool(r["decided_at"])) for r in _rows(hub_root, session)}
    assert st[row] == (0, False), st
