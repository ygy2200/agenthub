# -*- coding: utf-8 -*-
"""R824：ZCode 接入记忆钩子（免提醒注入的第七家）。

ZCode 的钩子配置与 Claude 系不同：钩子表在 hooks.events.<Event> 下一层、
配置钩子默认禁用（必须 hooks.enabled=true）、command 条目不允许 description
（严格白名单，混入即整条丢弃）。旧实现按 Claude 结构读写，zcode 未登记。
R822 旧断言「ZCode 不提供这两个事件」是误判：官方 diagnosing-hooks 文档
列出七个事件含 SessionStart/UserPromptSubmit，且其 Stop 钩子 10-25 前一直在跑。
"""
import json

import core


def _zcode_cfg(tmp_path, enabled=True):
    cfg = tmp_path / "config.json"
    data = {"mcp": {"servers": {}}, "plugins": [],
            "hooks": {"enabled": enabled, "events": {
                "Stop": [{"hooks": [{"type": "process", "command": "python.exe",
                                     "args": ["auto_log.py"], "timeoutMs": 15000}]}]}}}
    if not enabled:
        del data["hooks"]["enabled"]
    cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def _hooks_dir(tmp_path, monkeypatch):
    d = tmp_path / "hooks"
    d.mkdir(exist_ok=True)
    for n in (core.HOOK_BRIEF_NAME, core.HOOK_RECALL_NAME):
        (d / n).write_text("// stub\n", encoding="utf-8")
    monkeypatch.setattr(core, "hook_script_path", lambda name: d / name)
    return d


def test_zcode_dry_run_detects_events_nesting(tmp_path, monkeypatch):
    """预演：zcode 的钩子表在 hooks.events 下，探测必须穿进去，报 would_add 而非 bad_schema。"""
    cfg = _zcode_cfg(tmp_path)
    _hooks_dir(tmp_path, monkeypatch)
    r = core.install_brief_hook("zcode", path=str(cfg))
    assert r["status"] == "would_add", r
    assert r["event"] == "SessionStart"
    r2 = core.install_recall_hook("zcode", path=str(cfg))
    assert r2["status"] == "would_add", r2


def test_zcode_apply_writes_events_and_enabled(tmp_path, monkeypatch):
    """实装：写入 hooks.events.SessionStart、补 enabled=true、条目无 description。"""
    cfg = _zcode_cfg(tmp_path, enabled=False)
    _hooks_dir(tmp_path, monkeypatch)
    r = core.install_brief_hook("zcode", apply=True, path=str(cfg))
    assert r["status"] == "added", r
    data = json.loads(cfg.read_text(encoding="utf-8"))
    hooks = data["hooks"]
    assert hooks["enabled"] is True, "配置钩子默认禁用，必须显式开启"
    grp = hooks["events"]["SessionStart"]
    entry = grp[0]["hooks"][0]
    assert core.HOOK_BRIEF_NAME in entry["command"]
    assert "description" not in entry, "zcode schema 严格白名单，description 会整条丢弃"
    assert entry["statusMessage"]
    assert cfg.with_name(cfg.name + ".bak-agenthub-" + r["backup"].split("bak-agenthub-")[1]).is_file()


def test_zcode_install_idempotent(tmp_path, monkeypatch):
    """幂等：在位后再装报 already，不重复追加条目。"""
    cfg = _zcode_cfg(tmp_path)
    _hooks_dir(tmp_path, monkeypatch)
    assert core.install_brief_hook("zcode", apply=True, path=str(cfg))["status"] == "added"
    assert core.install_brief_hook("zcode", apply=True, path=str(cfg))["status"] == "already"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert len(data["hooks"]["events"]["SessionStart"]) == 1


def test_zcode_real_config_probe():
    """真实文件冒烟：本机 ~/.zcode/cli/config.json 结构可探测（探针钩子不影响 brain 脚本判定）。"""
    st = core.memory_hook_status("zcode")
    assert st["session"]["state"] in ("inplace", "unwired"), st
    assert st["prompt"]["state"] in ("inplace", "unwired"), st
    assert st["session"]["script"] and st["prompt"]["script"]
