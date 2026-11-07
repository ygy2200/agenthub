# -*- coding: utf-8 -*-
"""R971：报错召回钩子接住「exit 0 但 stdout 有 traceback」——端到端，先在旧码上红。

真缺口（R970 取证）：qoder 11-04 登记过 errors 表时间列是 ts（#20），本人今天又踩
`sqlite3.OperationalError: no such column: created`。真库实测 `brain.error_recall`
能命中 #20/#29、修法文本都生成好了，但钩子只在 `tool_response.isError` 为真时
放行；那条 Bash 用 `;` 串联，python 抛 traceback 而整条命令 exit 0 → 静默跳过。

用例直接以子进程喂 stdin 事件、检查 stdout 是否吐 `additionalContext`：
  · 真 traceback + 无错误标记 → 必须触发（旧码红）
  · 普通日志 "ERROR" 字样 / 只有 "Error:" 没有 Traceback 头 → 不许触发（防误报）
  · 只有 Traceback 头没有异常行 → 不许触发（信息不足，宁可漏）
  · 顶层 errorMessage 老路径 → 照常触发（回归守卫）
回归守卫不写 `isError=true + 只有 stdout`：那是 B045 的 JSON 拒收路径，
既有行为就是不触发，拿它当基线等于把老缺陷写成期望。

隔离：AGENTHUB_ROOT 指向临时库、NO_JOURNAL=1、STATE 落临时目录，不碰真库。
"""
import json
import os
import pathlib
import subprocess

import brain

NODE = r"D:/node/node.exe"
HOOK = (pathlib.Path(__file__).resolve().parents[2] / "brain_error_recall.mjs")


def _run(hook, tmp_path, response, extra=None):
    root = tmp_path / "hub"
    root.mkdir(exist_ok=True)
    brain.init_db(str(root))
    env = dict(os.environ)
    env.update({
        "AGENTHUB_ROOT": str(root),
        "AGENTHUB_ERROR_NO_JOURNAL": "1",
        "AGENTHUB_ERROR_STATE": str(tmp_path / "state.json"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    ev = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
          "session_id": "r971-test", "cwd": str(tmp_path),
          "tool_response": response}
    ev.update(extra or {})
    p = subprocess.run([NODE, str(hook), "--agent", "workbuddy"],
                       input=json.dumps(ev, ensure_ascii=False), text=True,
                       capture_output=True, timeout=25, env=env,
                       encoding="utf-8", errors="replace")
    return p.stdout or ""


def _has_context(out: str) -> bool:
    return "additionalContext" in out


def test_traceback_in_stdout_triggers_recall(tmp_path):
    """exit 0 场景：宿主没标 isError，但 stdout 有完整 Python traceback。"""
    tb = ("Command: python -c ...\n"
          "Traceback (most recent call last):\n"
          '  File "<stdin>", line 6, in <module>\n'
          "sqlite3.OperationalError: no such column: created\n"
          "=== 下一段命令 ===")
    resp = {"stdout": tb, "stderr": "", "isError": False}
    out = _run(HOOK, tmp_path, resp)
    assert _has_context(out), "结构化 traceback 必须触发报错召回：" + out[:300]


def test_plain_error_word_does_not_trigger(tmp_path):
    """防误报：普通日志里的 Error/ERROR 字样没有 Traceback 头，不许灌上下文。"""
    for body in ("2026-11-07 ERROR: 磁盘空间不足\n完成",
                 "Error: something went wrong\n继续执行",
                 "grep: error pattern not found in log"):
        out = _run(HOOK, tmp_path, {"stdout": body, "isError": False})
        assert not _has_context(out), f"误报：{body[:40]} → {out[:200]}"


def test_traceback_without_exception_line_does_not_trigger(tmp_path):
    """只有 Traceback 头、没有 XxxError: 异常行 → 信息不足，宁可漏也不误报。"""
    body = "Traceback (most recent call last):\n  File \"x.py\", line 1\n（被截断了）"
    out = _run(HOOK, tmp_path, {"stdout": body, "isError": False})
    assert not _has_context(out), out[:200]


def test_existing_error_message_path_still_works(tmp_path):
    """回归守卫：宿主在顶层给了 errorMessage 的老路径照常触发。"""
    out = _run(HOOK, tmp_path, {"isError": True, "stdout": ""},
               extra={"errorMessage":
                      "sqlite3.OperationalError: no such column: created_at"})
    assert _has_context(out), "老路径不该被新兜底破坏：" + out[:300]
