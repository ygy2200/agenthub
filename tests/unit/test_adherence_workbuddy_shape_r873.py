# -*- coding: utf-8 -*-
"""R873：workbuddy 的 transcript 形状必须能交采纳证据。

病根（线上实跑，不是假想）：Qoder/Claude 的 transcript 用 `text` / `tool_use`，
workbuddy 用 `input_text` / `output_text`，工具调用是独立的 `function_call` 事件
（没有 role）。旧解析器只认前者——实测会话 18e1b66f 的 66 个 assistant 事件全部
抽不到东西，Stop 日志长期是 `src=direct chars=600`（只有收尾一句话），判官饿着，
36 笔注入停在「未观测」而其中可能有真被用上的。

本文件钉住：
  1. `function_call` 的参数里的招牌 → 判成 used（真干活的地方）；
  2. `output_text` 正文也算证据（不是只有 `text`）；
  3. `reasoning` / `function_call_result` 不算证据（复述与工具输出都不是产出）；
  4. 轮边界照旧：上一轮的 function_call 不能替本轮领功。
"""
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MJS = REPO / "brain_adherence.mjs"
PY_ENTRY = REPO / "brain_adherence.py"
SIG = "zebra_probe_r873.py"          # 只在记忆与工具参数里出现的招牌
HOOK_KEY = "adoption_shape"
QUERY = f"{HOOK_KEY} 为什么 workbuddy 的采纳账一直饿着"
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


def _msg(role: str, text: str, ts: str) -> str:
    """workbuddy 的真实形状：role/content 在顶层，part 叫 input_text/output_text。"""
    kind = "input_text" if role == "user" else "output_text"
    return json.dumps({"type": "message", "role": role, "timestamp": ts,
                       "content": [{"type": kind, "text": text}]}, ensure_ascii=False)


def _call(name: str, args: dict, ts: str) -> str:
    """workbuddy 的工具调用：独立事件、无 role、arguments 是 JSON 字符串。"""
    return json.dumps({"type": "function_call", "name": name, "timestamp": ts,
                       "arguments": json.dumps(args, ensure_ascii=False)}, ensure_ascii=False)


def _result(name: str, text: str, ts: str) -> str:
    return json.dumps({"type": "function_call_result", "name": name, "timestamp": ts,
                       "output": {"type": "text", "text": text}}, ensure_ascii=False)


def _reasoning(text: str, ts: str) -> str:
    return json.dumps({"type": "reasoning", "timestamp": ts,
                       "rawContent": [{"type": "reasoning_text", "text": text}]},
                      ensure_ascii=False)


def _seed(hub_root: str, session: str, agent: str = "workbuddy") -> int:
    import brain
    brain.init_db(hub_root)
    for text in DISTRACTORS:
        brain.add_memory(hub_root, text, kind="lesson", tags="misc", agent="qoder")
    mid = brain.add_memory(hub_root,
                           f"{HOOK_KEY} 这类坑记在 {SIG}：工具的 arguments 里有招牌，"
                           f"但那不是 assistant 消息。",
                           kind="lesson", tags="r873 workbuddy", agent=agent)
    res = brain.prompt_recall(hub_root, QUERY, agent=agent, session=session,
                              update_state=False)
    assert mid in res["memory_ids"], res["memory_ids"]
    return mid


def _used(hub_root: str, session: str) -> int:
    db = Path(hub_root) / "_hub" / "brain.db"
    with sqlite3.connect(str(db)) as conn:
        return int(conn.execute("SELECT COALESCE(SUM(used),0) FROM recall_adherence"
                                " WHERE session=?", (session,)).fetchone()[0])


def _run_stop(tmp_path: Path, hub_root: str, payload: dict) -> subprocess.CompletedProcess:
    node = _node()
    if not node:
        pytest.skip("没有 node")
    env = {**os.environ,
           "AGENTHUB_ADHERENCE_SCRIPT": str(PY_ENTRY),
           "AGENTHUB_ROOT": hub_root,
           "PYTHONIOENCODING": "utf-8"}
    if os.name == "nt":
        env["USERPROFILE"] = str(tmp_path / "fake-home")
    else:
        env["HOME"] = str(tmp_path / "fake-home")
    return subprocess.run([node, str(MJS)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60, env=env)


def _transcript(tmp_path: Path, lines: list, name="wb.jsonl") -> str:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return str(p)


def test_workbuddy_function_call_is_evidence(hub_root, tmp_path):
    """招牌只在 function_call 的 arguments 里：这正是线上「真干活」的位置，必须判 used。"""
    session = "r873-call"
    _seed(hub_root, session)
    tp = _transcript(tmp_path, [
        _msg("user", QUERY, "2026-11-04T16:23:30.000Z"),
        _reasoning("用户要的是这个流程", "2026-11-04T16:23:31.000Z"),
        _call("Bash", {"command": f"D:/python311/python.exe {SIG} --json"},
              "2026-11-04T16:23:32.000Z"),
        _result("Bash", "跑完了", "2026-11-04T16:23:33.000Z"),
        _msg("assistant", "已经跑完了。", "2026-11-04T16:23:34.000Z"),
    ])
    r = _run_stop(tmp_path, hub_root, {"hook_event_name": "Stop", "session_id": session,
                                      "transcript_path": tp,
                                      "last_assistant_message": "已经跑完了。"})
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert _used(hub_root, session) >= 1, f"function_call 的参数没被当证据：{r.stderr}"


def test_workbuddy_output_text_is_evidence(hub_root, tmp_path):
    """正文 part 叫 output_text 也要认——旧版只认 text，整段回答都被丢掉。"""
    session = "r873-text"
    _seed(hub_root, session)
    tp = _transcript(tmp_path, [
        _msg("user", QUERY, "2026-11-04T16:23:30.000Z"),
        _msg("assistant", f"结论：按 {SIG} 的流程排查。", "2026-11-04T16:23:34.000Z"),
    ])
    r = _run_stop(tmp_path, hub_root, {"hook_event_name": "Stop", "session_id": session,
                                      "transcript_path": tp})
    assert r.returncode == 0, r.stderr
    assert _used(hub_root, session) >= 1, "output_text 正文没进判定"


def test_workbuddy_reasoning_and_result_are_not_evidence(hub_root, tmp_path):
    """reasoning 会复述注入原文、function_call_result 是读进来的东西——都不算产出。"""
    session = "r873-noise"
    _seed(hub_root, session)
    tp = _transcript(tmp_path, [
        _msg("user", QUERY, "2026-11-04T16:23:30.000Z"),
        _reasoning(f"注入里提到了 {SIG}，照着看看", "2026-11-04T16:23:31.000Z"),
        _result("Read", f"文件内容 {SIG}", "2026-11-04T16:23:32.000Z"),
        _msg("assistant", "看完了。", "2026-11-04T16:23:33.000Z"),
    ])
    r = _run_stop(tmp_path, hub_root, {"hook_event_name": "Stop", "session_id": session,
                                      "transcript_path": tp})
    assert r.returncode == 0, r.stderr
    assert _used(hub_root, session) == 0, "思考段/工具输出被当成了采纳证据"


def test_workbuddy_turn_boundary_holds(hub_root, tmp_path):
    """上一轮的 function_call 不能替本轮领功：轮边界按真人开口（input_text）切。"""
    session = "r873-boundary"
    _seed(hub_root, session)
    tp = _transcript(tmp_path, [
        _msg("user", "先做上一件事", "2026-11-04T16:20:00.000Z"),
        _call("Bash", {"command": f"python {SIG}"}, "2026-11-04T16:20:05.000Z"),
        _msg("user", QUERY, "2026-11-04T16:23:30.000Z"),
        _call("Bash", {"command": "python cli.py status"}, "2026-11-04T16:23:32.000Z"),
        _msg("assistant", "看完了。", "2026-11-04T16:23:34.000Z"),
    ])
    r = _run_stop(tmp_path, hub_root, {"hook_event_name": "Stop", "session_id": session,
                                      "transcript_path": tp})
    assert r.returncode == 0, r.stderr
    assert _used(hub_root, session) == 0, "上一轮的证据漏进本轮"
