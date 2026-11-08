# -*- coding: utf-8 -*-
"""R986 / B119：报错召回钩子接住「exit 0 但 stdout 是 Node 异常」——端到端，先在旧码上红。

真缺口（R984 当场实证）：
  · 非 Python 运行时**单独**抛异常 → 非零退出 → 宿主标 isError → 走 B045 正常通道，已覆盖；
  · 但 `node -e "throw ..." | head -3; echo done` 这类**串联/管道后接成功命令**会把整条
    命令的退出码变成 0，宿主不标错；而 R971 的兜底只认 Python 的
    `Traceback (most recent call last):`，Node 形状整段被静默跳过。
  · 真库旁证：`fail_events` 近 14 天 24 条 `exception_line` 的 head **全是 python 调用**，
    一条 Node 形状都没有——不是没发生，是抓不到。

判据必须双条件（只认一种形状会误报）：
  ① 一行 `XxxError: ...`（Error / TypeError / AssertionError…）；
  ② 该行前 6 行内有「只有 caret」的定位行，**或**后 4 行内有 `at …` 栈帧。
只回传错误行本身：不带 `[eval]:1`、不带文件路径、不带栈（签名要稳定，R865 口径）。

隔离同 R971：AGENTHUB_ROOT 指向临时库、NO_JOURNAL=1、STATE 落临时目录。
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
          "session_id": "r986-test", "cwd": str(tmp_path),
          "tool_response": response}
    ev.update(extra or {})
    p = subprocess.run([NODE, str(hook), "--agent", "workbuddy"],
                       input=json.dumps(ev, ensure_ascii=False), text=True,
                       capture_output=True, timeout=25, env=env,
                       encoding="utf-8", errors="replace")
    return p.stdout or ""


def _has_context(out: str) -> bool:
    return "additionalContext" in out


def test_node_error_with_caret_triggers(tmp_path):
    """caret 分支（栈被 `| head` 截断是常态）：只有定位行，没有 at 栈帧。

    错误行必须带招牌词（ENOENT / 路径），否则 python 侧判「无签名的失败」不开口——
    这是首版用例红的原因，不是钩子的问题（日志里 `node_error_from_stdout` 已经打出来了）。
    """
    body = ("[eval]:1\n"
            "const fs=require('fs'); fs.readFileSync('nope.json')\n"
            "^\n"
            "\n"
            "Error: ENOENT: no such file or directory, open 'nope.json'\n"
            "done")
    out = _run(HOOK, tmp_path, {"stdout": body, "isError": False})
    assert _has_context(out), "Node 异常（带 caret）必须触发报错召回：" + out[:300]


def test_node_error_with_stack_but_no_caret_triggers(tmp_path):
    """文件形式的 Node 报错：段落里可能没有 caret，但一定有 at 栈帧。"""
    body = ("/app/build/run.js:12\n"
            "const x = undefined.y;\n"
            "TypeError: Cannot read properties of undefined (reading 'y')\n"
            "    at run (/app/build/run.js:12:15)\n"
            "    at Object.<anonymous> (/app/build/run.js:20:1)\n"
            "done")
    out = _run(HOOK, tmp_path, {"stdout": body, "isError": False})
    assert _has_context(out), "Node 异常（带栈帧）必须触发：" + out[:300]


def test_plain_error_line_does_not_trigger(tmp_path):
    """防误报：普通日志里的 Error: 行既无 caret 也无栈帧，不许灌上下文。"""
    for body in ("Error: connect refused\n继续执行",
                 "2026-11-08 Error: 磁盘空间不足\n完成",
                 "grep: error pattern not found in log"):
        out = _run(HOOK, tmp_path, {"stdout": body, "isError": False})
        assert not _has_context(out), f"误报：{body[:30]!r} → {out[:200]}"


def test_caret_inside_character_class_does_not_trigger(tmp_path):
    """防误报：正则里的 `[^a]` 也含 ^，但不是 Node 的定位行（整行只有 caret 才算）。"""
    body = ("Error: bad pattern [^a]-z\n"
            "提示：用 [^abc] 取反\n"
            "完成")
    out = _run(HOOK, tmp_path, {"stdout": body, "isError": False})
    assert not _has_context(out), "字符类里的 ^ 被误当 caret 行：" + out[:200]


def test_python_traceback_still_wins(tmp_path):
    """回归守卫：Python traceback 走老路径，不被 Node 规则抢走（两条兜底共存）。"""
    body = ("Traceback (most recent call last):\n"
            '  File "<stdin>", line 2, in <module>\n'
            "ValueError: bad value\n"
            "done")
    out = _run(HOOK, tmp_path, {"stdout": body, "isError": False})
    assert _has_context(out), out[:300]
