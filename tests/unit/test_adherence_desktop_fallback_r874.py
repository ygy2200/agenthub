# -*- coding: utf-8 -*-
"""R874：桌面版不给 transcript_path 时，按 session id 从 projects 目录把会话文件找回来。

线上实测（2026-11-04，a3aa5b2a 本会话）：Qoder Desktop 的 UserPromptSubmit 载荷只有
session_id，没有 transcript_path。补扫钩子因此每句都打一行「skip sweep: 无 transcript」，
上一轮明明已经写进 `~/.qoder-cn/projects/<slug>/<session>.jsonl`（9.4MB），判定却永远
跑不到——桌面版这一家的注入只能停在「未观测」，adopt_count 拿不到它的反馈。

修法：事件缺 transcript_path 时，在 `~/.qoder-cn/projects/*/<session>.jsonl` 与
`~/.claude/projects/*/<session>.jsonl` 里按**文件名严格等于 session** 找最新一份。
只认同名文件：跨会话串证据比少判一笔更坏。钉住的行为：
  1. 事件无 transcript_path 时仍能首见判最后一轮（src=firstsight，日志带 transcript=projects）；
  2. 下一次发言照常判增量（src=sweep），水位线按同一文件推进；
  3. 找不到同名会话文件就静默（不猜、不误判）；
  4. 别的会话文件存在也不算数（文件名匹配是硬条件）。
"""
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MJS = REPO / "brain_adherence_sweep.mjs"
PY_ENTRY = REPO / "brain_adherence.py"
SIG = "zebra_probe_r874_desktop.py"
HOOK_KEY = "desktop_fallback"
QUERY = f"{HOOK_KEY} 桌面版不带 transcript 时怎么判"
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


def _line(role: str, content, minutes_ago: int = 10) -> str:
    ts = (datetime.utcnow() - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    return json.dumps({"type": role, "timestamp": ts,
                       "message": {"role": role, "content": content}},
                      ensure_ascii=False)


def _user(text: str, minutes_ago: int = 10) -> str:
    return _line("user", [{"type": "text", "text": text}], minutes_ago)


def _tool(name: str, command: str, minutes_ago: int = 9) -> str:
    return _line("assistant", [{"type": "tool_use", "name": name,
                                "input": {"command": command}}], minutes_ago)


def _seed(hub_root: str, session: str) -> int:
    import brain
    brain.init_db(hub_root)
    for text in DISTRACTORS:
        brain.add_memory(hub_root, text, kind="lesson", tags="misc", agent="qoder")
    mid = brain.add_memory(hub_root,
                           f"{HOOK_KEY} 这类坑记在 {SIG}：会话文件按 session id 落盘。",
                           kind="lesson", tags="r874 桌面补扫", agent="qoder")
    res = brain.prompt_recall(hub_root, QUERY, agent="qoder", session=session,
                              update_state=False)
    assert mid in res["memory_ids"], res["memory_ids"]
    return mid


def _run_sweep(tmp_path: Path, hub_root: str, payload: dict,
               agent: str = "qoder") -> subprocess.CompletedProcess:
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
    return subprocess.run([node, str(MJS), "--agent", agent], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60, env=env)


def _payload(session: str) -> dict:
    """线上桌面版形状：只有 session_id，没有 transcript_path。"""
    return {"hook_event_name": "UserPromptSubmit", "session_id": session,
            "prompt": "下一句", "cwd": str(REPO)}


def _desktop_transcript(tmp_path: Path, session: str, lines: list) -> Path:
    d = tmp_path / "fake-home" / ".qoder-cn" / "projects" / "D--Qoder-CN--qoder-versions"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return p


def _state(hub_root: str, session: str) -> dict:
    db = Path(hub_root) / "_hub" / "brain.db"
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT used, decided_at FROM recall_adherence WHERE session=?", (session,))]
        notes = [r[0] for r in conn.execute(
            "SELECT note FROM journal WHERE action='brain_adherence' ORDER BY id")]
    return {"rows": rows, "notes": notes}


def _log(tmp_path: Path) -> str:
    p = tmp_path / "fake-home" / ".agenthub" / "logs" / "brain-adherence.log"
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def test_desktop_first_sight_finds_project_transcript(hub_root, tmp_path):
    """无 transcript_path：按 session 找回 projects 下的会话文件，首见就判最后一轮。"""
    session = "r874-desktop-first"
    _seed(hub_root, session)
    _desktop_transcript(tmp_path, session, [
        _user(QUERY, minutes_ago=10),
        _tool("Bash", f"python {SIG} --json", minutes_ago=9),
    ])
    r = _run_sweep(tmp_path, hub_root, _payload(session))
    assert r.returncode == 0, r.stderr
    st = _state(hub_root, session)
    assert any(row["used"] for row in st["rows"]), f"没判最后一轮：{r.stderr}"
    assert any("src=firstsight" in n for n in st["notes"]), st["notes"]
    assert "transcript=projects" in _log(tmp_path), _log(tmp_path)[-300:]


def test_desktop_next_prompt_judges_delta(hub_root, tmp_path):
    """找回文件之后，下一句发言照常判增量（src=sweep），水位线按同一文件推进。"""
    session = "r874-desktop-next"
    mid = _seed(hub_root, session)
    tr = _desktop_transcript(tmp_path, session, [
        _user(QUERY, minutes_ago=10),
        _tool("Bash", "python cli.py status", minutes_ago=9),
    ])
    _run_sweep(tmp_path, hub_root, _payload(session))                     # 首见：立水位线
    with tr.open("a", encoding="utf-8", newline="\n") as f:
        f.write(_tool("Bash", f"D:/python311/python.exe {SIG} --json", minutes_ago=1) + "\n")
    r = _run_sweep(tmp_path, hub_root, _payload(session))
    assert r.returncode == 0, r.stderr
    st = _state(hub_root, session)
    assert any(row["used"] for row in st["rows"]), f"增量没判：{r.stderr}"
    assert any("src=sweep" in n for n in st["notes"]), st["notes"]
    import brain
    with brain.db_conn(hub_root) as conn:
        adopt = int(conn.execute("SELECT adopt_count FROM memories WHERE id=?",
                                 (mid,)).fetchone()[0])
    assert adopt == 1, f"同一笔注入被重复计采纳（adopt_count={adopt}）"


def _workbuddy_transcript(tmp_path: Path, session: str, lines: list) -> Path:
    d = tmp_path / "fake-home" / ".workbuddy" / "projects" / "d-workbuddy产出-2026-11-04"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return p


def _wb_event(kind: str, payload: dict, minutes_ago: int = 10) -> str:
    ts = (datetime.utcnow() - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    return json.dumps({"type": kind, "timestamp": ts, **payload}, ensure_ascii=False)


def test_workbuddy_projects_fallback_r876(hub_root, tmp_path):
    """R876：workbuddy 的会话也按 <slug>/<session>.jsonl 落盘，兜底必须覆盖它。"""
    session = "r876-wb-desktop"
    _seed(hub_root, session)
    _workbuddy_transcript(tmp_path, session, [
        _wb_event("message", {"role": "user", "content": [
            {"type": "input_text", "text": QUERY}]}, minutes_ago=10),
        _wb_event("function_call", {"name": "Bash", "arguments": json.dumps(
            {"command": f"python {SIG}"}, ensure_ascii=False)}, minutes_ago=9),
    ])
    r = _run_sweep(tmp_path, hub_root, _payload(session), agent="workbuddy")
    assert r.returncode == 0, r.stderr
    st = _state(hub_root, session)
    assert any(row["used"] for row in st["rows"]), \
        f"workbuddy 兜底没把证据交上来：{r.stderr}"
    assert any("src=firstsight" in n for n in st["notes"]), st["notes"]
    assert "transcript=projects" in _log(tmp_path), _log(tmp_path)[-300:]


def test_desktop_without_session_file_is_silent(hub_root, tmp_path):
    """projects 下没有同名会话文件：静默退出，不拿别的会话文件凑数、不交假账。"""
    session = "r874-desktop-missing"
    _seed(hub_root, session)
    _desktop_transcript(tmp_path, "some-other-session", [
        _user(QUERY), _tool("Bash", f"python {SIG}"),
    ])
    r = _run_sweep(tmp_path, hub_root, _payload(session))
    assert r.returncode == 0, r.stderr
    st = _state(hub_root, session)
    assert st["notes"] == [], "没有同名会话文件却交了账"
    assert all(not row["used"] for row in st["rows"])
    assert "projects 下也没找到" in _log(tmp_path), _log(tmp_path)[-300:]
