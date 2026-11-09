# -*- coding: utf-8 -*-
"""R1032：报错通道的正文来源要能分账（payload / transcript）。

背景：R1029 之前 WorkBuddy 的 PostToolUse 只给退出码，报错通道 100% 空转；修完后
transcript 兜底成了真路。**不量就不知道它是主路还是备胎**——下一步要不要调预算、
要不要把兜底推广到别家，全靠这个数。所以流水里带 `src=`，体检里单独报一行。

钉三件事：
  ① 有 transcript 来源时，体检必须报「正文来源：payload X / transcript Y」；
  ② 只有 payload 时不报这一行（别拿空分账当结论）；
  ③ 脚本确实会把 `--src` 写进流水 note（源码级钉住，防接线漂）。
"""
import datetime
import pathlib

import brain
import core
import ops


def _patch(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "HOOK_BRIEF_TARGETS", {"workbuddy": tmp_path / "mcp.json"})
    (tmp_path / "hermes.yml").write_text("hooks: {}\n", encoding="utf-8")
    monkeypatch.setattr(core, "HOOK_BRIEF_HERMES_TARGETS",
                        [("hermes", tmp_path / "hermes.yml")])
    d = tmp_path / "hooks"
    d.mkdir(exist_ok=True)
    for n in (core.HOOK_BRIEF_NAME, core.HOOK_RECALL_NAME, core.HOOK_BRIEF_HERMES_SCRIPT,
              core.HOOK_ADHERENCE_NAME, core.HOOK_ERROR_RECALL_NAME):
        (d / n).write_text("// stub\n", encoding="utf-8")
    monkeypatch.setattr(core, "hook_script_path", lambda name: d / name)
    mcp = tmp_path / "mcp"
    mcp.mkdir(exist_ok=True)
    for n in ("brain_brief.py", "brain_recall.py", "brain_adherence.py",
              "brain_error_recall.py"):
        (mcp / n).write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(core, "MCP_SERVER_DIR", mcp)
    cfg = {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": core.HOOK_BRIEF_NAME}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": core.HOOK_RECALL_NAME}]}],
        "Stop": [{"hooks": [{"type": "command", "command": core.HOOK_ADHERENCE_NAME}]}],
        "PostToolUse": [{"hooks": [{"type": "command",
                                    "command": core.HOOK_ERROR_RECALL_NAME}]}],
    }}
    (tmp_path / "mcp.json").write_text(__import__("json").dumps(cfg), encoding="utf-8")


def _seed(root, notes):
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    for note in notes:
        brain.journal_add(root, "workbuddy", "brain_error_recall", "tool_failure",
                         note=note, via="hook")


def test_reports_src_split(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    root = str(tmp_path / "hub")
    _seed(root, ["120 chars · tool=Bash · sig=operationalerror · src=transcript",
                 "90 chars · tool=Bash · sig=valueerror · src=payload"])
    res = ops._brief_hook_health(root)
    txt = "\n".join(res.get("detail") or [])
    assert "正文来源" in txt and "transcript 1" in txt, txt[:600]


def test_no_split_line_when_only_payload(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    root = str(tmp_path / "hub")
    _seed(root, ["90 chars · tool=Bash · sig=valueerror · src=payload"])
    res = ops._brief_hook_health(root)
    txt = "\n".join(res.get("detail") or [])
    assert "正文来源" not in txt, txt[:600]


def test_script_writes_src_into_journal():
    src = (pathlib.Path(__file__).resolve().parents[2] / "brain_error_recall.py").read_text(
        encoding="utf-8")
    assert "--src" in src and "src={args.src" in src, \
        "脚本没把 --src 写进流水 note，体检分账会一直是空的"
