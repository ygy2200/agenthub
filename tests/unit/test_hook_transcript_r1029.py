# -*- coding: utf-8 -*-
"""R1029 / B121：WorkBuddy 的 PostToolUse 事件里**没有 stdout/stderr 正文**。

现场取证（2026-11-09 12:22，本机 WorkBuddy 5.9.2 实捕）：
  · PreToolUse / PostToolUse 都会派发，settings.json 的接线没问题；
  · 但 `tool_response` 只有 `exitCode/signal/interrupted/sandboxDenied/…` 七个元数据键，
    没有 stdout、没有 stderr、没有 isError；
  · 于是 `errorText(ev)` 恒为空 → 钩子每次静默退出（日志里连 `skip:` 都不写），
    报错即时回忆在 WorkBuddy 上 100% 死掉；真库证据：`brain-error-recall.log`
    自 2026-11-08T13:01 之后再无一行记录。
  · 正文其实在会话 transcript 的 `function_call_result.output.text` 里，只是宿主在钩子
    返回之后才落盘（实测恒落后 1–2 次调用），所以按会话水位线跨调用续扫。

本用例只钉三件事：
  ① 事件无正文 + transcript 里有失败结果 → 必须出 additionalContext（src=transcript）；
  ② transcript 里那条结果是成功（Stderr (empty) / Exit Code 0）→ 不许开口；
  ③ 同一条结果第二次进来不许重复注入（水位线生效）。
"""
import json
import os
import pathlib
import subprocess
import time

import pytest

import brain
from _node_exe import node_exe

NODE = node_exe()  # R1119：改走统一解析器，不再写死 D 盘（CI runner 没这个盘符）
HOOK = (pathlib.Path(__file__).resolve().parents[2] / "brain_error_recall.mjs")

WB_RESPONSE = {"exitCode": 1, "signal": None, "interrupted": False,
               "sandboxDenied": False, "stderrBytesTruncated": 0,
               "stdoutBytesTruncated": 0, "tool_error_code": "1"}

FAIL_TEXT = (
    "Command: python -c \"import sys; sys.exit(1)\"\n"
    "Stdout: (empty)\n"
    "Stderr: Traceback (most recent call last):\n"
    '  File "<string>", line 1, in <module>\n'
    "    raise ValueError('bad value: r1029')\n"
    "ValueError: bad value: r1029\n"
    "Exit Code: 1\n"
    "Signal: (none)\n"
)

OK_TEXT = (
    "Command: echo hi\n"
    "Stdout: hi\n"
    "Stderr: (empty)\n"
    "Exit Code: 0\n"
    "Signal: (none)\n"
)


def _transcript(tmp_path, entries):
    p = tmp_path / "transcript.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return p


def _entry(call_id, text, ts_offset_ms=-800):
    ts = int(time.time() * 1000) + ts_offset_ms
    return [
        {"type": "function_call", "callId": call_id, "name": "Bash",
         "arguments": json.dumps({"command": "python -c \"import sys; sys.exit(1)\""}),
         "timestamp": ts},
        {"type": "function_call_result", "callId": call_id, "name": "Bash",
         "status": "completed", "timestamp": ts,
         "output": {"type": "text", "text": text}},
    ]


def _run(tmp_path, transcript, session="r1029-test"):
    if not NODE:
        pytest.skip("node 不可用：CI 需 setup-node，或设 AGENTHUB_NODE 指向 node.exe")
    root = tmp_path / "hub"
    root.mkdir(exist_ok=True)
    brain.init_db(str(root))
    env = dict(os.environ)
    env.update({
        "AGENTHUB_ROOT": str(root),
        "AGENTHUB_ERROR_NO_JOURNAL": "1",
        "AGENTHUB_ERROR_STATE": str(tmp_path / "state.json"),
        "AGENTHUB_ERROR_WM": str(tmp_path / "wm.json"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    ev = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
          "session_id": session, "cwd": str(tmp_path),
          "tool_input": {"command": "python -c \"import sys; sys.exit(1)\""},
          "transcript_path": str(transcript),
          "tool_response": WB_RESPONSE}
    p = subprocess.run([NODE, str(HOOK), "--agent", "workbuddy"],
                       input=json.dumps(ev, ensure_ascii=False), text=True,
                       capture_output=True, timeout=25, env=env,
                       encoding="utf-8", errors="replace")
    return p.stdout or ""


def test_workbuddy_shape_reads_error_from_transcript(tmp_path):
    """核心缺口：事件只给 exitCode，正文必须从 transcript 补。"""
    tr = _transcript(tmp_path, _entry("c1", FAIL_TEXT))
    out = _run(tmp_path, tr)
    assert "additionalContext" in out, "transcript 里的失败结果没被捞出来：" + out[:300]


def test_success_result_does_not_open_mouth(tmp_path):
    """防误报：transcript 里是成功结果（Exit 0 / Stderr (empty)）不许注入。"""
    tr = _transcript(tmp_path, _entry("c2", OK_TEXT))
    out = _run(tmp_path, tr)
    assert "additionalContext" not in out, "成功结果被当成报错：" + out[:300]


def test_same_result_not_injected_twice(tmp_path):
    """水位线：同一条 transcript 结果第二次调用不许再注一遍。"""
    tr = _transcript(tmp_path, _entry("c3", FAIL_TEXT))
    first = _run(tmp_path, tr)
    second = _run(tmp_path, tr)
    assert "additionalContext" in first, first[:300]
    assert "additionalContext" not in second, "同一结果重复注入：" + second[:300]
