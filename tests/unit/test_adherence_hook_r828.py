# -*- coding: utf-8 -*-
"""采纳回填钩子 R828：把「注入有没有被用」的采集从人工改成自动。

R827 把账本（recall_adherence）建好了，但采集要人手动 `--mark`，等于没有——真实
会话里没人会记得敲那一行，报表永远停在调试样本上。Stop 是「一轮结束」的确定出口：
那时判一次最准也最省（PostToolUse 一轮可能几十次，每次比对指纹纯属浪费）。

本文件钉住：
  1. Stop 条目要**追加**，不能顶掉别人已有的 Stop 钩子（claude 上就跑着 agent_guard）；
  2. 状态三态要能区分「装了」「没装」「hermes 没出口」，不能把 unsupported 报成缺；
  3. 钩子脚本拿不到产出时必须安静退出，绝不拦住会话；
  4. 真跑一次 Stop 事件能把 used 标上（端到端）。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import core

REPO = Path(__file__).resolve().parents[2]


def _cfg(tmp_path, name="settings.json"):
    p = tmp_path / name
    p.write_text(json.dumps({
        "theme": "dark",
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "existing.mjs"}]}],
                  "Stop": [{"hooks": [{"type": "command", "command": "stop.mjs"}]}]},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def test_answer_event_is_registered_as_stop():
    assert core.HOOK_JSON_EVENTS["answer"][0] == "Stop"
    assert core.HOOK_JSON_EVENTS["answer"][1] == core.HOOK_ADHERENCE_NAME


def test_apply_appends_after_existing_stop_hooks(tmp_path):
    p = _cfg(tmp_path)
    orig = json.loads(p.read_text(encoding="utf-8"))
    r = core.install_adherence_hook("claude", apply=True, path=str(p),
                                    node="D:/node/node.exe", install_script=False)
    assert r["status"] == "added", r
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["hooks"]["SessionStart"] == orig["hooks"]["SessionStart"], "别的事件不能动"
    stop = data["hooks"]["Stop"]
    assert len(stop) == 2, stop
    assert "stop.mjs" in stop[0]["hooks"][0]["command"], "原有 Stop 钩子必须保留"
    assert core.HOOK_ADHERENCE_NAME in stop[1]["hooks"][0]["command"]


def test_idempotent(tmp_path):
    p = _cfg(tmp_path)
    core.install_adherence_hook("claude", apply=True, path=str(p), install_script=False)
    again = core.install_adherence_hook("claude", apply=True, path=str(p),
                                        install_script=False)
    assert again["status"] == "already", again
    assert len(json.loads(p.read_text(encoding="utf-8"))["hooks"]["Stop"]) == 2


def test_status_reports_answer_and_hermes_unsupported(tmp_path):
    p = _cfg(tmp_path)
    st = core.memory_hook_status("claude", path=str(p))
    assert "answer" in st, st
    assert st["answer"]["state"] in ("unwired", "absent", "inplace"), st
    core.install_adherence_hook("claude", apply=True, path=str(p), install_script=False)
    st2 = core.memory_hook_status("claude", path=str(p))
    assert st2["answer"]["state"] == "inplace", st2
    h = core.memory_hook_status("hermes")
    assert h["answer"]["state"] == "unsupported", h


def test_hook_mark_wording():
    import cli
    full = {"session": {"state": "inplace"}, "prompt": {"state": "inplace"},
            "answer": {"state": "inplace"}}
    assert cli._hook_mark(full) == "✓简报+回忆+回填"
    hermes = {"session": {"state": "inplace"}, "prompt": {"state": "covered"},
              "answer": {"state": "unsupported"}}
    assert cli._hook_mark(hermes) == "✓简报+回忆（回填无出口）", cli._hook_mark(hermes)
    missing = {"session": {"state": "inplace"}, "prompt": {"state": "inplace"},
               "answer": {"state": "unwired"}}
    assert cli._hook_mark(missing) == "✗缺回填", cli._hook_mark(missing)


def _node() -> str:
    for c in ("D:/node/node.exe", shutil.which("node") or ""):
        if c and Path(c).is_file():
            return c
    return ""


def test_bundle_lists_adherence_files():
    files = core.bundle_files()
    assert "brain_adherence.py" in files, files
    assert "brain_adherence.mjs" in files, files


def test_hook_script_is_silent_without_text(tmp_path):
    """拿不到产出 = 静默退出，不能拦住会话。"""
    node = _node()
    if not node:
        import pytest
        pytest.skip("没有 node")
    script = REPO / "brain_adherence.mjs"
    ev = {"hook_event_name": "Stop", "session_id": "s-empty"}
    r = subprocess.run([node, str(script)], input=json.dumps(ev),
                       capture_output=True, text=True, timeout=30,
                       env={**os.environ, "AGENTHUB_ADHERENCE_SCRIPT": str(tmp_path / "nope.py")})
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stdout.strip() == "", r.stdout


def test_hook_script_ignores_non_stop_events():
    node = _node()
    if not node:
        import pytest
        pytest.skip("没有 node")
    script = REPO / "brain_adherence.mjs"
    for ev in ({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                "prompt": "hi"},
               {"hook_event_name": "PreToolUse", "session_id": "s1"}):
        r = subprocess.run([node, str(script)], input=json.dumps(ev),
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and r.stdout.strip() == "", (ev, r.stdout, r.stderr)


def test_end_to_end_marks_used(hub_root, tmp_path):
    """真跑：注入 → Stop 事件 → used 标上。"""
    import brain
    brain.init_db(hub_root)
    mid = brain.add_memory(
        hub_root, "release.bat 的括号要写成 ^( ^)，见 tests/unit/test_batch_block_parens_r802.py。",
        kind="lesson", tags="release.bat", agent="qoder")
    res = brain.prompt_recall(hub_root, "release.bat 括号怎么修", agent="claude",
                              session="e2e-828", update_state=False)
    assert mid in res["memory_ids"], (res["memory_ids"], res["text"])
    out = brain.mark_recall_used(
        hub_root, "e2e-828", "改好了：跑 tests/unit/test_batch_block_parens_r802.py 通过")
    assert out["matched"] >= 1 and mid in out["memory_ids"], out
