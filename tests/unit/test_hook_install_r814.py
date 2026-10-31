# -*- coding: utf-8 -*-
"""简报钩子安装器 R814：逐家手改配置既会漏也不可重放。

病根：R811 只手工给 Qoder 的 settings.json 挂了一条，claude/codex/workbuddy/dsh
都没装（医生实测三家 MCP 配置在但零调用）。手改无法幂等、无法审计、重装会重复挂。
本文件钉住安装器底线：默认预演不动文件、写前备份、只追加 SessionStart 一条、
保留原有条目与其它键、重复安装不叠条目、坏结构明确失败而不是覆盖。
"""
import json
from pathlib import Path

import core


def _cfg(tmp_path, name="settings.json", data=None):
    p = tmp_path / name
    p.write_text(json.dumps(data if data is not None else {
        "theme": "dark",
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "existing.mjs"}]}],
                  "Stop": [{"hooks": [{"type": "command", "command": "stop.mjs"}]}]},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def test_dry_run_never_touches_file(tmp_path):
    p = _cfg(tmp_path)
    before = p.read_bytes()
    r = core.install_brief_hook("claude", path=str(p), install_script=False)
    assert r["status"] == "would_add", r
    assert core.HOOK_BRIEF_NAME in r["command"]
    assert p.read_bytes() == before, "预演必须一个字节都不改"


def test_apply_appends_one_entry_backs_up_and_preserves_rest(tmp_path):
    p = _cfg(tmp_path)
    orig = json.loads(p.read_text(encoding="utf-8"))
    r = core.install_brief_hook("claude", apply=True, path=str(p), node="D:/node/node.exe",
                                install_script=False)
    assert r["status"] == "added", r
    assert r["backup"] and (tmp_path / Path(r["backup"]).name).is_file(), "写前必须留备份"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["theme"] == "dark", "其它键必须原样保留"
    assert data["hooks"]["Stop"] == orig["hooks"]["Stop"], "其它事件必须原样保留"
    sess = data["hooks"]["SessionStart"]
    assert len(sess) == 2 and core.HOOK_BRIEF_NAME in sess[1]["hooks"][0]["command"]
    assert "--agent claude" in sess[1]["hooks"][0]["command"]


def test_second_apply_is_idempotent(tmp_path):
    p = _cfg(tmp_path)
    core.install_brief_hook("codex", apply=True, path=str(p), install_script=False)
    r = core.install_brief_hook("codex", apply=True, path=str(p), install_script=False)
    assert r["status"] == "already", r
    data = json.loads(p.read_text(encoding="utf-8"))
    hits = [g for g in data["hooks"]["SessionStart"]
            if any(core.HOOK_BRIEF_NAME in str(h.get("command", "")) for h in g["hooks"])]
    assert len(hits) == 1, "重复安装不能叠加多条"


def test_missing_config_and_unknown_agent_are_reported_not_created(tmp_path):
    missing = tmp_path / "nope" / "settings.json"
    r = core.install_brief_hook("workbuddy", apply=True, path=str(missing), install_script=False)
    assert r["status"] == "no_config" and not missing.exists(), "不能凭空造配置文件"
    r = core.install_brief_hook("nobody", install_script=False)
    assert r["status"] == "unsupported", r


def test_bad_schema_fails_without_overwrite(tmp_path):
    p = _cfg(tmp_path, data={"hooks": []})
    before = p.read_bytes()
    r = core.install_brief_hook("dsh", apply=True, path=str(p), install_script=False)
    assert r["status"] == "bad_schema", r
    assert p.read_bytes() == before, "结构不符时不得覆盖用户配置"


def test_all_json_agents_are_registered():
    assert set(core.HOOK_BRIEF_TARGETS) == {"qoder", "claude", "codex", "workbuddy", "dsh", "zcode"}
