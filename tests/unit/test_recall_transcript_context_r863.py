# -*- coding: utf-8 -*-
"""R863：短句补上下文——「继续 / 接着弄 / 重启了，继续」也能接上刚才那件事。

病根：这类推进句整句都是停用词，`extract_prompt_terms` 抽不出检索词；R825 之后
只回「本项目近况」，等于知道项目做到哪、却不知道刚才在聊哪一件。宿主在
UserPromptSubmit 里本来就有 transcript_path（R851 补扫钩子同源），读尾部几条
用户真说的话就够——本轮把这条数据用在召回侧。

钉住的边界：
  1. 只认 role=user 的 text part；工具结果 / 注入块 / 紧凑摘要 / 中断标记不进上下文；
  2. 当前句自己能抽词时，上下文**不参与**检索（避免把旧话题硬塞进新问题）；
  3. 没有 transcript / 抽不出词时，行为与 R825 完全一致（只给项目近况）；
  4. 钩子侧：没 transcript 的短句仍然静默（R820 契约不放宽）。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import brain

REPO = Path(__file__).resolve().parents[2]


def _line(role, text=None, parts=None):
    content = parts if parts is not None else [{"type": "text", "text": text}]
    return json.dumps({"type": role, "message": {"role": role, "content": content}},
                      ensure_ascii=False)


def _transcript(tmp_path, rows):
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return p


def _seed(root):
    brain.init_db(root)
    return brain.add_memory(
        root,
        "Windows 批处理：release.bat 失败分支的 exit code 必须显式 exit /b 0，"
        "否则沿用上一条命令的残值。",
        kind="lesson", tags="release.bat,退出码,失败分支", agent="qoder")


def test_context_reads_only_real_user_text(tmp_path):
    tp = _transcript(tmp_path, [
        _line("user", "帮我把 release.bat 失败分支的退出码修一下"),
        _line("assistant", "好的，先看 release.bat"),
        _line("user", parts=[{"type": "tool_result", "content": "exit code 255"}]),
        _line("user", "【AgentHub 即时回忆 · 自动检索，无需提醒】\n注入块不是人话"),
        _line("user", "[Request interrupted by user for tool use]"),
        _line("user", "This session is being continued from a previous conversation. "
                      "The summary below covers the earlier portion."),
        _line("user", "继续"),
        "{ 半截 json",
    ])
    ctx = brain.recall_context_prompt(str(tp), current_prompt="继续")
    assert ctx.strip() == "帮我把 release.bat 失败分支的退出码修一下", ctx


def test_context_limit_keeps_newest_messages(tmp_path):
    rows = [_line("user", f"第{n}件事：release.bat 参数 {n}") for n in range(1, 7)]
    tp = _transcript(tmp_path, rows)
    ctx = brain.recall_context_prompt(str(tp), limit=2)
    assert "第5件事" in ctx and "第6件事" in ctx and "第4件事" not in ctx, ctx


def test_vague_prompt_uses_context_to_recall(hub_root, tmp_path):
    mid = _seed(hub_root)
    res = brain.prompt_recall(
        hub_root, "继续", agent="qoder", session="ctx", state_path=str(tmp_path / "s.json"),
        context_text="帮我把 release.bat 失败分支的退出码修一下")
    assert res["context_used"] is True, res
    assert mid in res["memory_ids"], (res["memory_ids"], res["text"])
    assert "按上一轮话题接续" in res["text"], res["text"]


def test_explicit_prompt_ignores_context(hub_root, tmp_path):
    mid = _seed(hub_root)
    other = brain.add_memory(hub_root, "红色沙漠：删 DLSSEnabler 只动游戏目录内的副本。",
                             kind="lesson", tags="红色沙漠,DLSSEnabler", agent="qoder")
    res = brain.prompt_recall(
        hub_root, "红色沙漠的 DLSSEnabler 文件怎么删", agent="qoder", session="new",
        state_path=str(tmp_path / "n.json"),
        context_text="release.bat 失败分支的退出码怎么修")
    assert res["context_used"] is False, res
    assert other in res["memory_ids"] and mid not in res["memory_ids"], res["memory_ids"]


def test_no_context_keeps_project_only_fallback(hub_root, tmp_path):
    _seed(hub_root)
    brain.add_record(hub_root, "演示-发布", "qoder", "2026-11-04",
                     "修 release.bat 失败分支", "R863 探针")
    res = brain.prompt_recall(hub_root, "继续", project="演示-发布",
                              session="p", state_path=str(tmp_path / "p.json"))
    assert res["context_used"] is False
    assert res["text"] == "" or "项目近况" in res["text"] or "本项目近况" in res["text"], res


def test_node_hook_passes_transcript_for_short_prompt(hub_root, tmp_path):
    node = shutil.which("node") or (
        "D:/node/node.exe" if Path("D:/node/node.exe").is_file() else "")
    if not node:
        import pytest
        pytest.skip("本机无 node，跳过钩子契约实测")
    mid = _seed(hub_root)
    tp = _transcript(tmp_path, [
        _line("user", "帮我把 release.bat 失败分支的退出码修一下"),
        _line("assistant", "先查那次的结论"),
    ])
    ev = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "继续",
                     "cwd": hub_root, "session_id": "r863-node",
                     "transcript_path": str(tp)}, ensure_ascii=False)
    env = {**os.environ,
           "AGENTHUB_RECALL_SCRIPT": str(REPO / "brain_recall.py"),
           "AGENTHUB_PYTHON": sys.executable,
           "AGENTHUB_ROOT": hub_root,
           "AGENTHUB_RECALL_STATE": str(tmp_path / "node-state.json"),
           "AGENTHUB_RECALL_NO_JOURNAL": "1"}
    r = subprocess.run([node, str(REPO / "brain_recall.mjs"), "--agent", "qoder"],
                       input=ev, capture_output=True, text=True, encoding="utf-8",
                       timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "按上一轮话题接续" in ctx and "批处理" in ctx, ctx
    assert str(mid) in ctx or "release.bat" in ctx.lower(), ctx


def test_node_hook_short_prompt_without_transcript_still_silent(hub_root, tmp_path):
    """推进句会被放行给 Python，但没有会话文件、也没有项目近况时仍然静默。"""
    node = shutil.which("node") or (
        "D:/node/node.exe" if Path("D:/node/node.exe").is_file() else "")
    if not node:
        import pytest
        pytest.skip("本机无 node，跳过钩子契约实测")
    _seed(hub_root)
    ev = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "继续",
                     "cwd": hub_root, "session_id": "r863-silent"}, ensure_ascii=False)
    r = subprocess.run([node, str(REPO / "brain_recall.mjs"), "--agent", "qoder"],
                       input=ev, capture_output=True, text=True, encoding="utf-8",
                       timeout=30,
                       env={**os.environ, "AGENTHUB_ROOT": hub_root,
                            "AGENTHUB_TRANSCRIPT_HOME": str(tmp_path / "no-home"),
                            "AGENTHUB_RECALL_SCRIPT": str(REPO / "brain_recall.py"),
                            "AGENTHUB_PYTHON": sys.executable,
                            "AGENTHUB_RECALL_STATE": str(tmp_path / "silent.json"),
                            "AGENTHUB_RECALL_NO_JOURNAL": "1"})
    assert r.returncode == 0 and r.stdout.strip() == "", (r.stdout, r.stderr)


def test_find_transcript_by_session_id(tmp_path):
    """Qoder Desktop 的载荷不带 transcript_path，但会话文件以 session_id 命名。"""
    import brain_recall
    p = tmp_path / ".qoder-cn" / "projects" / "C--Users-y-demo" / "abcd1234-uuid.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("{}\n", encoding="utf-8")
    assert brain_recall.find_transcript("abcd1234-uuid", home=str(tmp_path)) == str(p)
    assert brain_recall.find_transcript("tiny", home=str(tmp_path)) == ""
    assert brain_recall.find_transcript("zzzz9999-uuid", home=str(tmp_path)) == ""


def test_cli_discovers_transcript_when_hook_payload_lacks_it(hub_root, tmp_path):
    """端到端：没有 --transcript 时，按 session_id 找到会话文件并补上下文。"""
    mid = _seed(hub_root)
    sid = "abcd1234-uuid-r863"
    home = tmp_path / "home"
    tp = home / ".qoder-cn" / "projects" / "C--Users-y-demo" / f"{sid}.jsonl"
    tp.parent.mkdir(parents=True)
    tp.write_text("\n".join([
        _line("user", "帮我把 release.bat 失败分支的退出码修一下"),
        _line("assistant", "先查那次的结论"),
    ]) + "\n", encoding="utf-8")
    env = {**os.environ, "AGENTHUB_TRANSCRIPT_HOME": str(home),
           "AGENTHUB_ROOT": hub_root, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, str(REPO / "brain_recall.py"), "--root", hub_root,
         "--agent", "qoder", "--prompt", "继续", "--session", sid,
         "--json", "--no-journal", "--no-state", "--origin", "probe"],
        capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    assert res["context_used"] is True, res
    assert mid in res["memory_ids"], (res["memory_ids"], res["text"])
    assert "按上一轮话题接续" in res["text"]
    with brain.db_conn(hub_root) as conn:
        n = conn.execute("SELECT inject_count FROM memories WHERE id=?", (mid,)).fetchone()[0]
    assert int(n or 0) == 0, "探针口径不许把曝光账记脏（R861）"
