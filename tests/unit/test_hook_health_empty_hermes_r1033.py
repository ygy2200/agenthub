# -*- coding: utf-8 -*-
"""R1033：钩子体检在 hermes 目标表为空时不许整条崩掉。

真实现场：写 R1032 用例时把 `HOOK_BRIEF_HERMES_TARGETS` patcha 成空表，体检直接
`KeyError: 0`——因为取法写死成 `[0][1]`。生产上它通常非空，但「新装环境只装了别家」
「以后把 hermes 目标表改成配置驱动」都会让这条路径变成真崩溃，而崩溃的姿势是
**体检挂了**：读的人会去修钩子，实际是巡检自己的下标。空表就跳过这一家。

钉两条：
  ① 空表不抛异常，返回值仍是有 ok/detail 结构的结果；
  ② 空表时不许因为「没检查 hermes」而报「钩子丢失」。
"""
import pathlib

import brain
import core
import ops


def _patch(monkeypatch, tmp_path, hermes_targets):
    monkeypatch.setattr(core, "HOOK_BRIEF_TARGETS", {"workbuddy": tmp_path / "mcp.json"})
    monkeypatch.setattr(core, "HOOK_BRIEF_HERMES_TARGETS", hermes_targets)
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
    import json
    cfg = {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": core.HOOK_BRIEF_NAME}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": core.HOOK_RECALL_NAME}]}],
        "Stop": [{"hooks": [{"type": "command", "command": core.HOOK_ADHERENCE_NAME}]}],
        "PostToolUse": [{"hooks": [{"type": "command",
                                    "command": core.HOOK_ERROR_RECALL_NAME}]}],
    }}
    (tmp_path / "mcp.json").write_text(json.dumps(cfg), encoding="utf-8")


def test_empty_hermes_targets_does_not_crash(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path, [])
    root = str(tmp_path / "hub")
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    res = ops._brief_hook_health(root)      # 旧代码在这里 KeyError: 0
    assert isinstance(res, dict) and "detail" in res, repr(res)[:400]
    txt = "\n".join(res.get("detail") or [])
    # 没检查 hermes 不等于 hermes 钩子丢了：空表时不许把它写进「钩子丢失」名单
    for line in (res.get("detail") or []):
        if line.startswith("钩子丢失："):
            assert "hermes" not in line, line
    assert txt is not None
