# -*- coding: utf-8 -*-
"""R873：补扫首见会话时要判「最后一轮」，但只判这一轮开口之后的注入。

病根（线上实测）：第一句话的注入发生在 transcript 落盘之前——SessionStart/首句的
UserPromptSubmit 拿不到文件，补扫第一次真正看见会话时水位线直接跳到文件尾，
**第一轮的注入永远没人判**。实例 a3aa5b2a：16:39 注入 3 条 → 16:59 首见跳尾 →
整局停在「未观测」。

修法两条一起钉：
  1. 首见时把尾部最后一轮判掉（而不是白跳到尾）；
  2. 用那一轮 user 行的开口时间做下界（`--since`）——R851 那条「历史旧账不借尸还魂」
     的纪律不破：注入早于开口时，不许被这一轮的文本领功。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MJS = REPO / "brain_adherence_sweep.mjs"
PY_ENTRY = REPO / "brain_adherence.py"
SIG = "zebra_probe_r873_sweep.py"
HOOK_KEY = "first_sight"
QUERY = f"{HOOK_KEY} 首见会话时上一轮怎么补判"
DISTRACTORS = (
    "打包产物用 build.bat，upx 关掉否则杀软误报。",
    "GUI 无头渲染要设 QT_QPA_PLATFORM=offscreen。",
    "部署一致性用 check deploy_diff 比对 md5。",
    "记忆被取代别删，写新条目注明取代记忆#N。",
    "流水审计链用 prev_hash 串起来，改一条就断链。",
    "钩子脚本必须 fail-open，异常一律 exit 0。",
)


def _node() -> str:
    for c in (os.environ.get("AGENTHUB_NODE", ""), "D:/node/node.exe", shutil.which("node") or ""):
        if c and Path(c).is_file():
            return c
    return ""


def _line(role: str, content, ts: str = "") -> str:
    ev = {"type": role, "message": {"role": role, "content": content}}
    if ts:
        ev["timestamp"] = ts
    return json.dumps(ev, ensure_ascii=False)


def _user(text: str, minutes_ago: int = 10) -> str:
    ts = (datetime.utcnow() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return _line("user", [{"type": "text", "text": text}], ts)


def _tool(name: str, command: str, minutes_ago: int = 9) -> str:
    ts = (datetime.utcnow() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return _line("assistant", [{"type": "tool_use", "name": name,
                                "input": {"command": command}}], ts)


def _seed(hub_root: str, session: str) -> int:
    import brain
    brain.init_db(hub_root)
    for text in DISTRACTORS:
        brain.add_memory(hub_root, text, kind="lesson", tags="misc", agent="qoder")
    mid = brain.add_memory(hub_root,
                           f"{HOOK_KEY} 这类坑记在 {SIG}：第一轮注入要能被补扫看见。",
                           kind="lesson", tags="r873 首见", agent="qoder")
    res = brain.prompt_recall(hub_root, QUERY, agent="qoder", session=session,
                              update_state=False)
    assert mid in res["memory_ids"], res["memory_ids"]
    return mid


def _age_injection(hub_root: str, session: str, hours: int = 3) -> None:
    """把注入时间往前挪：模拟「旧会话留下的账」，用来验证 --since 下界。"""
    import brain
    ts = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE recall_adherence SET ts=? WHERE session=?", (ts, session))


def _run_sweep(tmp_path: Path, hub_root: str, payload: dict) -> subprocess.CompletedProcess:
    node = _node()
    if not node:
        pytest.skip("没有 node")
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ,
           "AGENTHUB_ADHERENCE_SCRIPT": str(PY_ENTRY),
           "AGENTHUB_ROOT": hub_root,
           "AGENTHUB_SWEEP_SYNC": "1",
           "PYTHONIOENCODING": "utf-8",
           "USERPROFILE": str(fake_home), "HOME": str(fake_home)}
    return subprocess.run([node, str(MJS), "--agent", "qoder"], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60, env=env)


def _payload(session: str, transcript: Path) -> dict:
    return {"hook_event_name": "UserPromptSubmit", "session_id": session,
            "transcript_path": str(transcript), "prompt": "下一句",
            "cwd": str(REPO)}


def _state(hub_root: str, session: str) -> dict:
    db = Path(hub_root) / "_hub" / "brain.db"
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT ts, fp, used, decided_at FROM recall_adherence WHERE session=?",
            (session,))]
        notes = [r[0] for r in conn.execute(
            "SELECT note FROM journal WHERE action='brain_adherence' ORDER BY id")]
    return {"rows": rows, "notes": notes}


def test_first_sight_judges_last_turn(hub_root, tmp_path):
    """首见：尾部最后一轮有招牌 → 判 used，并把 src=firstsight 写进流水。"""
    session = "r873-first-judge"
    _seed(hub_root, session)
    tr = tmp_path / "s.jsonl"
    tr.write_text(_user(QUERY, minutes_ago=10) + "\n"
                  + _tool("Bash", f"python {SIG} --json", minutes_ago=9) + "\n",
                  encoding="utf-8", newline="\n")
    r = _run_sweep(tmp_path, hub_root, _payload(session, tr))
    assert r.returncode == 0, r.stderr
    st = _state(hub_root, session)
    assert any(row["used"] for row in st["rows"]), f"首见没判最后一轮：{r.stderr}"
    assert any("src=firstsight" in n for n in st["notes"]), st["notes"]


def test_first_sight_does_not_credit_old_injections(hub_root, tmp_path):
    """首见只判开口之后的注入：3 小时前的旧账不许被这一轮的文本领功。"""
    session = "r873-first-old"
    _seed(hub_root, session)
    _age_injection(hub_root, session, hours=3)
    tr = tmp_path / "old.jsonl"
    tr.write_text(_user("新的这一轮", minutes_ago=10) + "\n"
                  + _tool("Bash", f"python {SIG} --json", minutes_ago=9) + "\n",
                  encoding="utf-8", newline="\n")
    r = _run_sweep(tmp_path, hub_root, _payload(session, tr))
    assert r.returncode == 0, r.stderr
    st = _state(hub_root, session)
    assert all(not row["used"] for row in st["rows"]), "旧注入被本轮文本领了功"
    assert all(not (row["decided_at"] or "").strip() for row in st["rows"]), \
        "早于开口的注入不该被判过（它不属于这一轮）"


def test_first_sight_without_prompt_boundary_is_silent(hub_root, tmp_path):
    """尾部只有产出、找不到真人开口：宁可少判一笔，不把半段历史当整轮。"""
    session = "r873-first-nobound"
    _seed(hub_root, session)
    tr = tmp_path / "nobound.jsonl"
    tr.write_text(_tool("Bash", f"python {SIG}", minutes_ago=5) + "\n",
                  encoding="utf-8", newline="\n")
    r = _run_sweep(tmp_path, hub_root, _payload(session, tr))
    assert r.returncode == 0, r.stderr
    st = _state(hub_root, session)
    assert st["notes"] == [], "没有开口边界却交了账"
    assert all(not row["used"] for row in st["rows"])
    assert list((tmp_path / "fake-home" / ".agenthub" / "state").glob("adh-sweep-*.json")), \
        "水位线没落地，下一次会重复读"


def test_not_before_bounds_mark_recall_used(hub_root):
    """纯函数下界：早于 not_before 的行不动，之后的行照判（判无用也写 decided_at）。"""
    import brain
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "下界测试记忆。", kind="lesson", tags="r873")
    now = datetime.now()
    old = (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S")
    new = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    fp = json.dumps(["zebra_probe_r873.py"])
    with brain.db_conn(hub_root) as conn:
        for ts in (old, new):
            conn.execute("INSERT INTO recall_adherence(ts,session,agent,memory_id,project,"
                         "strength,fp) VALUES(?,?,?,?,?,?,?)",
                         (ts, "r873-bound", "qoder", mid, "", "strong", fp))
    out = brain.mark_recall_used(hub_root, "r873-bound", "无关文本",
                                 not_before=(now - timedelta(minutes=30)).strftime(
                                     "%Y-%m-%dT%H:%M:%S"))
    assert out["checked"] == 1, out
    with brain.db_conn(hub_root) as conn:
        rows = [tuple(r) for r in conn.execute(
            "SELECT ts, decided_at FROM recall_adherence WHERE session=? ORDER BY ts",
            ("r873-bound",))]
    assert not (rows[0][1] or "").strip(), "早于下界的行被判了"
    assert (rows[1][1] or "").strip(), "下界之后的行没被判"
