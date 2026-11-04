# -*- coding: utf-8 -*-
"""R878：zcode 的注入也能被判定——它的会话不在 projects 下。

病根：zcode 把会话写成 `~/.zcode/cli/rollout/model-io-<session>.jsonl`（一行一次模型
调用，含全量历史），文件名多了 `model-io-` 前缀、位置也不在 `projects/<slug>/` 下。
R876 的回填只认 `<session>.jsonl`，于是 zcode 的 3 笔注入永远停在「判不了」——量具
对这家等于失明（报表：判不了 3 · 未观测 3）。

修法：
  1. `find_transcript` 在三家 projects 都找不到时，回落到 zcode rollout 的精确文件名；
  2. `turns_from_events` 认 `type:model_io`：`turnId` 换值即新轮，`startedAt` 是开口，
     证据取 `response.text` + `response.toolCalls[].input`（`reasoningText` 不算）；
  3. 区间纪律不变：每轮只判 [本轮开口, 下一轮开口)，后一轮不许替前一轮领功。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import brain

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import adherence_backfill as bf  # noqa: E402


def _ins(root: str, ts: str, session: str, mid: int, fp: list,
         agent: str = "zcode") -> int:
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


def _zrec(turn: str, dt_local: datetime, text: str = "", calls: list | None = None) -> dict:
    return {
        "type": "model_io",
        "turnId": turn,
        "startedAt": _utc_z(dt_local),
        "completedAt": _utc_z(dt_local + timedelta(seconds=5)),
        "response": {
            "text": text,
            "reasoningText": "",
            "toolCalls": [{"name": n, "input": a} for n, a in (calls or [])],
        },
    }


def _write_zcode(home: Path, session: str, records: list) -> Path:
    d = home / ".zcode" / "cli" / "rollout"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"model-io-{session}.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                 encoding="utf-8")
    return p


def _rows(root: str, session: str) -> dict:
    with brain.db_conn(root) as conn:
        return {r["id"]: (r["used"], bool(r["decided_at"])) for r in conn.execute(
            "SELECT id, used, decided_at FROM recall_adherence WHERE session=?", (session,))}


def test_zcode_transcript_is_found_by_model_io_name(hub_root, tmp_path):
    """三家 projects 都没有时，按 model-io-<session>.jsonl 精确找回。"""
    brain.init_db(hub_root)
    session = "session_zeta878"
    home = tmp_path / "fake-home"
    p = _write_zcode(home, session, [_zrec("turn-a", datetime.now() - timedelta(hours=2),
                                           "看完了")])
    empty = tmp_path / "projects"
    empty.mkdir()
    assert bf.find_transcript(session, [empty], home=home) == p
    assert bf.find_transcript("session_not_there", [empty], home=home) is None


def test_zcode_two_turns_do_not_cross_credit(hub_root, tmp_path):
    """turnId 切轮 + 区间纪律：第二轮的证据不能替第一轮的注入领功。"""
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "zcode 回填测试。", kind="lesson", tags="r878")
    session = "session_zeta878_turns"
    t0 = datetime.now() - timedelta(hours=3)
    t1 = datetime.now() - timedelta(hours=2)
    home = tmp_path / "fake-home"
    projects = tmp_path / "projects"
    projects.mkdir()
    _write_zcode(home, session, [
        _zrec("turn-a", t0, "先看看", [("Bash", {"command": "cat zeta878.py"})]),
        _zrec("turn-a", t0 + timedelta(seconds=20), "看完了"),
        _zrec("turn-b", t1, "下一轮", [("Bash", {"command": "python cli.py status"})]),
    ])
    row_a = _ins(hub_root, _local(t0 + timedelta(seconds=5)), session, mid, ["zeta878.py"])
    row_b = _ins(hub_root, _local(t0 + timedelta(seconds=6)), session, mid, ["omega878.py"])
    row_c = _ins(hub_root, _local(t1 + timedelta(seconds=5)), session, mid, ["omega878.py"])
    out = bf.run(hub_root, [{"session": session, "agent": "zcode"}],
                 [projects], apply=True, days=7, min_idle_h=0, home=home)
    assert out["sessions"][0]["status"] == "ok", out
    assert out["sessions"][0]["turns"] == 2, out
    st = _rows(hub_root, session)
    assert st[row_a] == (1, True), st        # 第一轮工具参数命中
    assert st[row_b] == (0, True), st        # 第一轮判过：无 omega，第二轮不许回判
    assert st[row_c] == (0, True), st        # 第二轮判过：没有 omega（status 那轮不相关）


def test_zcode_reasoning_is_not_evidence(hub_root, tmp_path):
    """reasoningText 里出现招牌不算采纳：注入原文就在同条消息里，复述≠照做。"""
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "zcode reasoning 测试。", kind="lesson", tags="r878")
    session = "session_zeta878_reason"
    t0 = datetime.now() - timedelta(hours=2)
    home = tmp_path / "fake-home"
    projects = tmp_path / "projects"
    projects.mkdir()
    rec = _zrec("turn-r", t0, "没动手")
    rec["response"]["reasoningText"] = "用户想要的是 gamma878.py 这类探针……"
    _write_zcode(home, session, [rec])
    row = _ins(hub_root, _local(t0 + timedelta(seconds=5)), session, mid, ["gamma878.py"])
    out = bf.run(hub_root, [{"session": session, "agent": "zcode"}],
                 [projects], apply=True, days=7, min_idle_h=0, home=home)
    assert out["sessions"][0]["checked"] == 1, out
    assert _rows(hub_root, session)[row] == (0, True)


def test_zcode_last_turn_without_evidence_is_skipped(hub_root, tmp_path):
    """最后一轮空产出（模型调用失败/中止）不许写 decided_at——没证据就不判。"""
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "zcode 空轮测试。", kind="lesson", tags="r878")
    session = "session_zeta878_empty"
    t0 = datetime.now() - timedelta(hours=2)
    home = tmp_path / "fake-home"
    projects = tmp_path / "projects"
    projects.mkdir()
    _write_zcode(home, session, [_zrec("turn-e", t0, "")])
    row = _ins(hub_root, _local(t0 + timedelta(seconds=5)), session, mid, ["zeta878.py"])
    out = bf.run(hub_root, [{"session": session, "agent": "zcode"}],
                 [projects], apply=True, days=7, min_idle_h=0, home=home)
    assert out["sessions"][0]["checked"] == 0, out
    assert _rows(hub_root, session)[row] == (0, False)
