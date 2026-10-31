# -*- coding: utf-8 -*-
"""R821：记忆钩子的部署闭环。

R820 把「按句回忆」装进了五家 JSON agent，但默认脚本路径指向
~/.agenthub/mcp_server/brain_recall.py——它没进 SERVER_FILES，部署不出来，
钩子装了也静默空转。exe 同样：PyInstaller onefile 只带 --add-data 的文件，
钩子脚本不进包，桌面版的安装按钮复制 0 个文件。本文件钉住这两条供应链。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import core

REPO = Path(__file__).resolve().parents[2]


def test_runtime_dir_prefers_pyinstaller_meipass(monkeypatch):
    assert core._runtime_dir() == REPO
    monkeypatch.setattr(sys, "_MEIPASS", str(REPO / "fake-mei"), raising=False)
    assert core._runtime_dir() == REPO / "fake-mei"


def test_bundle_contains_server_entries_and_hook_scripts(tmp_path):
    import cli
    assert "brain_recall.py" in core.SERVER_FILES
    assert "brain_recall.py" in core.bundle_files()
    assert "brain_recall.mjs" in core.bundle_files()
    out = tmp_path / "bundle"
    assert cli.cmd_bundle(str(out)) == 0
    for rel in core.bundle_files():
        assert (out / rel).is_file(), rel
    assert (out / "my" / "work" / "__init__.py").is_file()


def test_memory_hook_status_reports_both_events(tmp_path, monkeypatch):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for n in (core.HOOK_BRIEF_NAME, core.HOOK_RECALL_NAME):
        (hooks / n).write_text("// stub\n", encoding="utf-8")
    monkeypatch.setattr(core, "hook_script_path", lambda name: hooks / name)
    full = tmp_path / "full.json"
    full.write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": core.HOOK_BRIEF_NAME}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": core.HOOK_RECALL_NAME}]}],
    }}), encoding="utf-8")
    monkeypatch.setattr(core, "HOOK_BRIEF_TARGETS", {"qoder": str(full)})
    st = core.memory_hook_status("qoder")
    assert st["session"]["state"] == "inplace", st
    assert st["prompt"]["state"] == "inplace", st
    assert st["session"]["script"] and st["prompt"]["script"]

    brief_only = tmp_path / "brief.json"
    brief_only.write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": core.HOOK_BRIEF_NAME}]}],
    }}), encoding="utf-8")
    monkeypatch.setattr(core, "HOOK_BRIEF_TARGETS", {"qoder": str(brief_only)})
    st = core.memory_hook_status("qoder")
    assert st["session"]["state"] == "inplace"
    assert st["prompt"]["state"] == "unwired", st


def test_hermes_prompt_is_covered_by_same_pre_llm_call(tmp_path, monkeypatch):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / core.HOOK_BRIEF_HERMES_SCRIPT).write_text(
        "// uses brain_recall.py after the first brief\n", encoding="utf-8")
    monkeypatch.setattr(core, "hook_script_path", lambda name: hooks / name)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("hooks:\n  pre_llm_call:\n    - command: \"node brain_inject_hermes.mjs\"\n",
                   encoding="utf-8")
    monkeypatch.setattr(core, "HOOK_BRIEF_HERMES_TARGETS", (("hermes-cn", str(cfg)),))
    st = core.memory_hook_status("hermes-cn")
    assert st["session"]["state"] == "inplace", st
    assert st["prompt"]["state"] == "covered", st
    # 只写一条配置：同一脚本既发简报又做回忆
    rows = core.install_memory_hooks("hermes-cn", apply=True, path=str(cfg),
                                     install_script=False)
    assert rows["session"]["status"] == "already"
    assert rows["prompt"]["status"] == "already" and rows["prompt"]["covered"] is True
    assert cfg.read_text(encoding="utf-8").count("pre_llm_call:") == 1


def test_hermes_node_brief_then_recall_with_prompt_dedupe(tmp_path):
    node = shutil.which("node") or ("D:/node/node.exe" if Path("D:/node/node.exe").is_file() else "")
    if not node:
        import pytest
        pytest.skip("本机无 node，跳过钩子契约实测")
    brief = tmp_path / "brief_stub.py"
    brief.write_text("print('BRIEF-STUB')\n", encoding="utf-8")
    recall = tmp_path / "recall_stub.py"
    recall.write_text("print('RECALL-STUB')\n", encoding="utf-8")
    env = {**os.environ,
           "AGENTHUB_BRIEF_SCRIPT": str(brief),
           "AGENTHUB_RECALL_SCRIPT": str(recall),
           "AGENTHUB_PYTHON": sys.executable,
           "AGENTHUB_ROOT": str(tmp_path),
           "AGENTHUB_HERMES_STATE": str(tmp_path / "state.json"),
           "AGENTHUB_HERMES_LOG": str(tmp_path / "hook.log")}
    script = str(REPO / "brain_inject_hermes.mjs")

    def run(ev):
        return subprocess.run([node, script, "--agent", "hermes-cn"],
                              input=json.dumps(ev, ensure_ascii=False), capture_output=True,
                              text=True, encoding="utf-8", timeout=30, env=env)

    first = run({"hook_event_name": "pre_llm_call", "session_id": "s1",
                 "user_message": "第一次说话先给我上下文"})
    assert first.returncode == 0, first.stderr
    ctx = json.loads(first.stdout)["context"]
    assert "BRIEF-STUB" in ctx and "RECALL-STUB" in ctx, ctx

    dup = run({"hook_event_name": "pre_llm_call", "session_id": "s1",
               "user_message": "第一次说话先给我上下文"})
    assert dup.returncode == 0, dup.stderr
    assert dup.stdout.strip() == "", "同一句话 30 分钟内不应重复注入"

    nxt = run({"hook_event_name": "pre_llm_call", "session_id": "s1",
               "user_message": "换一句话，只回忆不再发简报"})
    assert nxt.returncode == 0, nxt.stderr
    ctx = json.loads(nxt.stdout)["context"]
    assert "RECALL-STUB" in ctx and "BRIEF-STUB" not in ctx, ctx
