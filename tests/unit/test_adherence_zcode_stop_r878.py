# -*- coding: utf-8 -*-
"""R878：zcode 的 Stop 载荷不带 transcript_path 时，按 session 找回 rollout 会话。

病根：zcode 的会话文件是 `~/.zcode/cli/rollout/model-io-<session>.jsonl`，Stop 载荷
既没有 transcript_path、也没有可当证据的文本字段（线上日志：zcode 的 Stop 从未写出
一行 brain_adherence）。结果这家注入的账永远停在「判不了」。

修法：事件缺 transcript_path 时按 session id 找 rollout 文件，按 `turnId` 取最后一轮，
证据 = `response.text` + `response.toolCalls[].input`；`reasoningText` 不算。
本文件钉住：
  1. 没有 transcript_path 也能判本轮（src=transcript）；
  2. 只看最后一轮：上一轮的招牌不能替本轮领功；
  3. 找不到同名文件就静默，不交假账。
"""
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MJS = REPO / "brain_adherence.mjs"
PY_ENTRY = REPO / "brain_adherence.py"
SIG = "zebra_stop_r878.py"
HOOK_KEY = "zcode_stop"
QUERY = f"{HOOK_KEY} 这类会话怎么判"
DISTRACTORS = (
    "打包产物用 build.bat，upx 关掉否则杀软误报。",
    "GUI 无头渲染要设 QT_QPA_PLATFORM=offscreen。",
    "部署一致性用 check deploy_diff 比对 md5。",
    "记忆被取代别删，写新条目注明取代记忆#N。",
    "流水审计链用 prev_hash 串起来，改一条就断链。",
    "钩子脚本必须 fail-open，异常一律 exit 0。",
)


def _node() -> str:
    for c in (os.environ.get("AGENTHUB_NODE", ""), "D:/node/node.exe",
              shutil.which("node") or ""):
        if c and Path(c).is_file():
            return c
    return ""


def _seed(hub_root: str, session: str) -> int:
    import brain
    brain.init_db(hub_root)
    for text in DISTRACTORS:
        brain.add_memory(hub_root, text, kind="lesson", tags="misc", agent="zcode")
    mid = brain.add_memory(hub_root, f"{HOOK_KEY} 这类坑记在 {SIG}：Stop 不带 transcript。",
                           kind="lesson", tags="r878 zcode", agent="zcode")
    res = brain.prompt_recall(hub_root, QUERY, agent="zcode", session=session,
                              update_state=False)
    assert mid in res["memory_ids"], res["memory_ids"]
    return mid


def _zrec(turn: str, text: str, calls: list, seconds_ago: int) -> dict:
    dt = datetime.now() - timedelta(seconds=seconds_ago)
    z = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
    return {"type": "model_io", "turnId": turn,
            "startedAt": z(dt), "completedAt": z(dt + timedelta(seconds=3)),
            "response": {"text": text, "reasoningText": "",
                         "toolCalls": [{"name": n, "input": a} for n, a in calls]}}


def _write_zcode(tmp_path: Path, session: str, records: list) -> Path:
    d = tmp_path / "fake-home" / ".zcode" / "cli" / "rollout"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"model-io-{session}.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                 encoding="utf-8", newline="\n")
    return p


def _run_stop(tmp_path: Path, hub_root: str, payload: dict) -> subprocess.CompletedProcess:
    node = _node()
    if not node:
        pytest.skip("没有 node")
    env = {**os.environ,
           "AGENTHUB_ADHERENCE_SCRIPT": str(PY_ENTRY),
           "AGENTHUB_ROOT": hub_root,
           "PYTHONIOENCODING": "utf-8",
           "USERPROFILE": str(tmp_path / "fake-home"),
           "HOME": str(tmp_path / "fake-home")}
    return subprocess.run([node, str(MJS), "--agent", "zcode"],
                          input=json.dumps(payload), capture_output=True,
                          text=True, timeout=60, env=env)


def _payload(session: str) -> dict:
    """线上 zcode Stop 形状：只有 session_id，没有 transcript_path。"""
    return {"hook_event_name": "Stop", "session_id": session}


def _used(hub_root: str, session: str) -> list:
    db = Path(hub_root) / "_hub" / "brain.db"
    with sqlite3.connect(str(db)) as conn:
        return [r[0] for r in conn.execute(
            "SELECT used FROM recall_adherence WHERE session=?", (session,))]


def _log(tmp_path: Path) -> str:
    p = tmp_path / "fake-home" / ".agenthub" / "logs" / "brain-adherence.log"
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def test_zcode_stop_without_transcript_path_judges_last_turn(hub_root, tmp_path):
    """无 transcript_path：按 session 找回 rollout，最后一轮工具参数里的招牌判成 used。"""
    session = "session_r878_stop_hit"
    _seed(hub_root, session)
    _write_zcode(tmp_path, session, [
        _zrec("turn-a", "先看看", [("Bash", {"command": "python cli.py status"})], 120),
        _zrec("turn-b", "收尾", [("Bash", {"command": f"D:/python311/python.exe {SIG}"})], 60),
    ])
    r = _run_stop(tmp_path, hub_root, _payload(session))
    assert r.returncode == 0, r.stderr
    assert any(_used(hub_root, session)), f"zcode 最后一轮没判：{r.stdout}{r.stderr}"
    assert "src=transcript" in _log(tmp_path), _log(tmp_path)[-300:]


def test_zcode_previous_turn_does_not_take_credit(hub_root, tmp_path):
    """只看最后一轮：上一轮碰巧用过的招牌不能替本轮领功。"""
    session = "session_r878_stop_prev"
    _seed(hub_root, session)
    _write_zcode(tmp_path, session, [
        _zrec("turn-a", "", [("Bash", {"command": f"python {SIG}"})], 120),
        _zrec("turn-b", "这轮没碰它", [("Bash", {"command": "python cli.py status"})], 60),
    ])
    r = _run_stop(tmp_path, hub_root, _payload(session))
    assert r.returncode == 0, r.stderr
    assert not any(_used(hub_root, session)), "旧轮证据漏进本轮"


def test_zcode_missing_rollout_is_silent(hub_root, tmp_path):
    """没有同名 rollout 文件：静默退出，不拿别的会话凑数、不交假账。"""
    session = "session_r878_stop_missing"
    _seed(hub_root, session)
    _write_zcode(tmp_path, "session_some_other", [_zrec("turn-x", "无关", [], 60)])
    r = _run_stop(tmp_path, hub_root, _payload(session))
    assert r.returncode == 0, r.stderr
    assert not any(_used(hub_root, session))
    assert "skip:" in _log(tmp_path), _log(tmp_path)[-300:]
