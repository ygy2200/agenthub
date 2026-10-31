# -*- coding: utf-8 -*-
"""R811：简报 CLI 入口与 SessionStart 钩子契约。

钩子是「AI 不提醒也会用大脑」的唯一确定性通道——它一旦静默失败，用户看到的
就是「模型又不读记忆」。所以这里同时钉两层：Python 入口能产出简报；node 钩子
输出的 JSON 能被客户端识别（additionalContext），且失败时静默 exit 0。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import brain
import brain_brief

REPO = Path(__file__).resolve().parents[2]


def _seed(root):
    brain.init_db(root)
    brain.add_record(root, "演示-简报", "qoder", "2026-10-30",
                     "2026-10-30（qoder）· 简报落库", "目的：验证\n做了什么：入口\n验证结果：单测")
    brain.add_memory(root, "钩子注入必须 fail-open", kind="lesson", pinned=True)


def test_python_entry_prints_brief(hub_root, capsys):
    _seed(hub_root)
    rc = brain_brief.main(["--root", hub_root, "--agent", "qoder", "--cwd",
                           f"{hub_root}/演示-简报", "--no-journal"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "AgentHub 大脑简报" in out and "演示-简报" in out


def test_python_entry_is_silent_on_bad_root(hub_root, capsys):
    rc = brain_brief.main(["--root", f"{hub_root}/不存在", "--agent", "qoder", "--no-journal"])
    out = capsys.readouterr().out
    assert rc == 0 and out.strip() == ""


def test_node_hook_emits_additional_context(hub_root, tmp_path):
    node = shutil.which("node") or ("D:/node/node.exe" if Path("D:/node/node.exe").is_file() else "")
    if not node:
        import pytest
        pytest.skip("本机无 node，跳过钩子契约实测")
    _seed(hub_root)
    ev = json.dumps({"hook_event_name": "SessionStart", "cwd": f"{hub_root}/演示-简报",
                     "session_id": "r811-test"}, ensure_ascii=False)
    env = {**__import__("os").environ, "AGENTHUB_BRIEF_SCRIPT": str(REPO / "brain_brief.py"),
           "AGENTHUB_PYTHON": sys.executable, "AGENTHUB_BRIEF_MAX_CHARS": "1200",
           "AGENTHUB_ROOT": hub_root}
    r = subprocess.run([node, str(REPO / "brain_inject.mjs"), "--agent", "qoder"],
                       input=ev, capture_output=True, text=True, encoding="utf-8",
                       timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    hook = payload["hookSpecificOutput"]
    assert hook["hookEventName"] == "SessionStart"
    ctx = hook["additionalContext"]
    assert "AgentHub 大脑简报" in ctx and "演示-简报" in ctx
    assert len(ctx) <= 1200, len(ctx)


def test_node_hook_ignores_non_session_events(hub_root):
    node = shutil.which("node") or ("D:/node/node.exe" if Path("D:/node/node.exe").is_file() else "")
    if not node:
        import pytest
        pytest.skip("本机无 node，跳过钩子契约实测")
    r = subprocess.run([node, str(REPO / "brain_inject.mjs"), "--agent", "qoder"],
                       input=json.dumps({"hook_event_name": "PreToolUse"}),
                       capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == ""
