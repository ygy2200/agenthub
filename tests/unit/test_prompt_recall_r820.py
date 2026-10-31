# -*- coding: utf-8 -*-
"""即时回忆 R820：用户每说一句话，按这句话自动检索记忆并注入。

病根：R811 的 SessionStart 简报只在开局送达一次；用户后来说「上次那个发布脚本
怎么修的」时，AI 仍要主人提醒「先查大脑」。本文件钉住召回的底线：
  - 查询词要干净（中文不切出「支的/的退」这类跨词 bigram）；
  - 真命中的教训要排到泛化偏好前面（意图权重）；
  - 只在命中时注入，同会话不重复；预算、失败降级、计数与流水都要正确。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import core

REPO = Path(__file__).resolve().parents[2]


def _seed(root):
    import brain
    brain.init_db(root)
    lesson = brain.add_memory(
        root, "Windows 批处理：if 块内 echo 的括号必须写成 ^( ^)，否则 cmd 先解析整块。"
              "现场是 release.bat 失败分支报 . was unexpected at this time.，发布成功后仍 255。",
        kind="lesson", tags="失败分支,release.bat,退出码", agent="qoder")
    pref = brain.add_memory(root, "用户偏好：合理的后续交付自主推进，不要天天问。",
                            kind="preference", pinned=True)
    other = brain.add_memory(root, "红色沙漠：删 DLSS Enabler 时只动游戏目录内的副本。",
                             kind="lesson", tags="红色沙漠,DLSS")
    rec = brain.add_record(root, "演示-发布", "qoder", "2026-10-30",
                           "2026-10-30（qoder）· 修 release.bat 失败分支",
                           "R802：release.bat 失败分支退出码与括号转义；验证：探针 0/1 两分支")
    return lesson, pref, other, rec


def test_prompt_terms_are_clean_and_keep_identifiers():
    import brain
    terms = [t for t, _ in brain.extract_prompt_terms(
        "发布脚本 release.bat 失败分支的退出码怎么修的？")]
    assert "release" in terms and "bat" in terms and "失败分支" in terms
    assert "支的" not in terms and "的退" not in terms
    assert brain.extract_prompt_terms("把红色沙漠的DLSS文件删掉")
    assert [t for t, _ in brain.extract_prompt_terms("把红色沙漠的DLSS文件删掉")][:2] == [
        "红色沙漠", "dlss"]
    assert brain.extract_prompt_terms("继续") == []
    assert brain.extract_prompt_terms("/hook-install") == []


def test_relevant_lesson_beats_generic_pinned_preference(hub_root, tmp_path):
    import brain
    lesson, pref, _, rec = _seed(hub_root)
    res = brain.prompt_recall(hub_root, "release.bat 失败分支的退出码怎么修的",
                              agent="qoder", session="s1",
                              state_path=str(tmp_path / "state.json"))
    assert res["text"], res
    assert res["memory_ids"][0] == lesson, (res["memory_ids"], pref, res["text"])
    assert "批处理" in res["text"]
    assert res["record_ids"] and rec in res["record_ids"]
    assert res["text"].startswith("【AgentHub 即时回忆")
    assert "无需提醒" in res["text"]
    assert res["elapsed_ms"] >= 0


def test_tags_count_as_content(hub_root, tmp_path):
    import brain
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "正文不写关键词，只在标签里标。",
                           kind="lesson", tags="失败分支,release.bat")
    res = brain.prompt_recall(hub_root, "release.bat 失败分支怎么修", agent="qoder",
                              session="tag", state_path=str(tmp_path / "s.json"))
    assert mid in res["memory_ids"], (res["memory_ids"], res["text"])


def test_same_session_dedupes_but_new_session_recalls_again(hub_root, tmp_path):
    import brain
    lesson, _, _, _ = _seed(hub_root)
    state = str(tmp_path / "state.json")
    q = "release.bat 失败分支怎么修"
    first = brain.prompt_recall(hub_root, q, agent="qoder", session="same",
                                state_path=state)
    assert lesson in first["memory_ids"]
    second = brain.prompt_recall(hub_root, q, agent="qoder", session="same",
                                 state_path=state)
    assert lesson not in second["memory_ids"], "同一会话不应反复注入同一条"
    third = brain.prompt_recall(hub_root, q, agent="qoder", session="other",
                                state_path=state)
    assert lesson in third["memory_ids"], "换会话应重新可用"


def test_use_count_counts_only_injected_rows(hub_root, tmp_path):
    import brain
    lesson, pref, _, _ = _seed(hub_root)

    def counts():
        with brain.db_conn(hub_root) as conn:
            return {r["id"]: int(r["use_count"] or 0) for r in conn.execute(
                "SELECT id, use_count FROM memories")}

    before = counts()
    brain.prompt_recall(hub_root, "release.bat 失败分支怎么修", agent="qoder",
                        session="u", state_path=str(tmp_path / "u.json"))
    after = counts()
    assert after[lesson] == before[lesson] + 1
    assert after[pref] == before[pref], "没被注入的记忆不该计唤起"


def test_budget_is_hard_and_no_hit_is_silent(hub_root, tmp_path):
    import brain
    _seed(hub_root)
    res = brain.prompt_recall(hub_root, "release.bat 失败分支怎么修", agent="qoder",
                              session="b", state_path=str(tmp_path / "b.json"),
                              max_chars=120)
    assert 0 < len(res["text"]) <= 120, len(res["text"])
    assert res["text"].endswith("已截断）")
    none = brain.prompt_recall(hub_root, "zzzz qqqq", agent="qoder",
                               session="n", state_path=str(tmp_path / "n.json"))
    assert none["text"] == "", none
    assert brain.prompt_recall("", "release.bat")["text"] == ""


def test_node_hook_emits_user_prompt_additional_context(hub_root, tmp_path):
    node = shutil.which("node") or ("D:/node/node.exe" if Path("D:/node/node.exe").is_file() else "")
    if not node:
        import pytest
        pytest.skip("本机无 node，跳过钩子契约实测")
    _seed(hub_root)
    ev = json.dumps({"hook_event_name": "UserPromptSubmit",
                     "prompt": "release.bat 失败分支的退出码怎么修",
                     "cwd": hub_root, "session_id": "r820-node"}, ensure_ascii=False)
    env = {**os.environ,
           "AGENTHUB_RECALL_SCRIPT": str(REPO / "brain_recall.py"),
           "AGENTHUB_PYTHON": sys.executable,
           "AGENTHUB_ROOT": hub_root,
           "AGENTHUB_RECALL_STATE": str(tmp_path / "node-state.json"),
           "AGENTHUB_RECALL_NO_JOURNAL": "1",
           "AGENTHUB_RECALL_MAX_CHARS": "900"}
    r = subprocess.run([node, str(REPO / "brain_recall.mjs"), "--agent", "qoder"],
                       input=ev, capture_output=True, text=True, encoding="utf-8",
                       timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    hook = payload["hookSpecificOutput"]
    assert hook["hookEventName"] == "UserPromptSubmit"
    assert "AgentHub 即时回忆" in hook["additionalContext"]
    assert "批处理" in hook["additionalContext"]


def test_node_hook_ignores_other_events_and_short_prompts(hub_root):
    node = shutil.which("node") or ("D:/node/node.exe" if Path("D:/node/node.exe").is_file() else "")
    if not node:
        import pytest
        pytest.skip("本机无 node，跳过钩子契约实测")
    for ev in ({"hook_event_name": "SessionStart", "prompt": "release.bat 失败分支"},
               {"hook_event_name": "UserPromptSubmit", "prompt": "继续"}):
        r = subprocess.run([node, str(REPO / "brain_recall.mjs"), "--agent", "qoder"],
                           input=json.dumps(ev, ensure_ascii=False),
                           capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert r.returncode == 0 and r.stdout.strip() == "", (ev, r.stdout, r.stderr)


def _cfg(tmp_path, data=None):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(data if data is not None else {
        "theme": "dark",
        "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command",
                                                  "command": "existing-guard.mjs"}]}],
                  "Stop": [{"hooks": [{"type": "command", "command": "stop.mjs"}]}]},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def test_recall_hook_installer_is_idempotent_and_preserves_existing(tmp_path):
    p = _cfg(tmp_path)
    orig = json.loads(p.read_text(encoding="utf-8"))
    r = core.install_recall_hook("workbuddy", apply=True, path=str(p),
                                 node="D:/node/node.exe", install_script=False)
    assert r["status"] == "added" and r["event"] == "UserPromptSubmit", r
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["hooks"]["Stop"] == orig["hooks"]["Stop"]
    assert data["hooks"]["UserPromptSubmit"][0] == orig["hooks"]["UserPromptSubmit"][0]
    cmds = [h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert any(core.HOOK_RECALL_NAME in c and "--agent workbuddy" in c for c in cmds)
    again = core.install_recall_hook("workbuddy", apply=True, path=str(p),
                                     install_script=False)
    assert again["status"] == "already"


def test_install_memory_hooks_installs_both_events(tmp_path):
    p = tmp_path / "hooks.json"
    p.write_text('{"hooks": {}}', encoding="utf-8")
    rows = core.install_memory_hooks("claude", apply=True, path=str(p),
                                     install_script=False)
    assert rows["session"]["status"] == "added" and rows["prompt"]["status"] == "added"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "SessionStart" in data["hooks"] and "UserPromptSubmit" in data["hooks"]
    bad = tmp_path / "bad.json"
    bad.write_text('{"hooks": []}', encoding="utf-8")
    assert core.install_recall_hook("dsh", apply=True, path=str(bad),
                                    install_script=False)["status"] == "bad_schema"
