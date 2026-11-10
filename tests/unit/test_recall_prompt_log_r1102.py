# -*- coding: utf-8 -*-
"""R1102：可选原句采集 —— 默认关零回归、开档脱敏、失败 fail-open。

验收走 brain_recall.main() 生产形状（monkeypatch prompt_recall 与 core.journal），
而不是只测纯函数，避免记忆 #511「夹具不是生产形状等于没测」。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain_recall as br  # noqa: E402
import core  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    old = br.RECALL_LOG_PROMPT
    yield
    br.RECALL_LOG_PROMPT = old


def test_default_is_off():
    assert br.RECALL_LOG_PROMPT is False


def test_redact_email_token_path_and_truncate():
    s = ("请把 a.b@qq.com 和 ghp_abcdefghijklmnopqrstuvwxyz123456 "
         "写到 C:\\Users\\y\\secret\\note.txt 顺便发我 " + "啊" * 300)
    out = br.redact_prompt(s)
    assert "<email>" in out and "<token>" in out and "<path>" in out
    assert "a.b@qq.com" not in out and "secret" not in out
    assert len(out) <= br.RECALL_LOG_PROMPT_CHARS


def test_redact_handles_empty_and_weird():
    assert br.redact_prompt("") == ""
    assert br.redact_prompt(None) == ""
    assert br.redact_prompt("  换行\n\t 压  缩  ") == "换行 压 缩"


def _run(monkeypatch, prompt, enabled):
    """跑一次真实 main()，返回被写下的 note（没有则 None）。"""
    br.RECALL_LOG_PROMPT = enabled
    monkeypatch.setattr(
        br.brain, "prompt_recall",
        lambda *a, **k: {"text": "【注入】", "memory_ids": [1],
                         "record_ids": [], "terms": ["测试"],
                         "context_used": False})
    captured = {}

    def fake_journal(root, agent, tool, action, note="", **kw):
        captured["note"] = note

    monkeypatch.setattr(core, "journal", fake_journal)
    monkeypatch.setattr(br, "find_transcript", lambda *a, **k: "")
    rc = br.main(["--root", str(ROOT), "--prompt", prompt,
                  "--no-state", "--no-journal", "--origin", "probe",
                  "--json"])
    return rc, captured.get("note")


def test_off_note_has_no_q_and_matches_legacy(monkeypatch):
    rc, note = _run(monkeypatch, "2026-10-25 那次调研结论是什么", False)
    # --no-journal 让 main 跳过写流水；本用例只验不抛异常，
    # 另用开档路径单独验证 note 形状。
    assert rc == 0


def test_on_note_contains_redacted_prompt(monkeypatch):
    br.RECALL_LOG_PROMPT = True
    monkeypatch.setattr(
        br.brain, "prompt_recall",
        lambda *a, **k: {"text": "x", "memory_ids": [], "record_ids": [],
                         "terms": [], "context_used": False})
    captured = {}
    monkeypatch.setattr(core, "journal",
                        lambda root, agent, tool, action, note="", **kw:
                        captured.setdefault("note", note))
    monkeypatch.setattr(br, "find_transcript", lambda *a, **k: "")
    # 去掉 --no-journal 才会走到写流水分支
    rc = br.main(["--root", str(ROOT), "--prompt", "联系 me@x.com 查上周记录",
                  "--no-state", "--origin", "probe", "--json"])
    assert rc == 0
    note = captured.get("note", "")
    assert "q=联系 <email> 查上周记录" in note
    assert "origin=probe" in note


def test_off_note_is_legacy_verbatim(monkeypatch):
    br.RECALL_LOG_PROMPT = False
    monkeypatch.setattr(
        br.brain, "prompt_recall",
        lambda *a, **k: {"text": "x", "memory_ids": [], "record_ids": [],
                         "terms": [], "context_used": False})
    captured = {}
    monkeypatch.setattr(core, "journal",
                        lambda root, agent, tool, action, note="", **kw:
                        captured.setdefault("note", note))
    monkeypatch.setattr(br, "find_transcript", lambda *a, **k: "")
    br.main(["--root", str(ROOT), "--prompt", "任意一句",
             "--no-state", "--origin", "probe", "--json"])
    note = captured.get("note", "")
    assert "q=" not in note
    assert note.startswith("1 chars · mem=[] · ctx=0 · terms= · origin=probe · session=")
