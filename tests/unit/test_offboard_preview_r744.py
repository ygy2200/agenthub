# -*- coding: utf-8 -*-
"""R744：offboard 预演没回答「影响谁 / 怎么回滚」，还有个拼写粘连。

实测（真机预演 zcode）：
    下线预演（未写入；加 --apply 执行）：ZCode
      · 移除引导块 -> ~/AGENTS.md（当前 v6）
      · 移除MCP 条目 -> ~/.zcode/cli/config.json          ← 「移除MCP」粘连
      两处都会先备份，失败自动还原。执行：python cli.py offboard zcode --apply

预演存在的意义就是让人判断「按不按」。它当时只说了改哪两处 + 一句「会备份」，
既没说下线后这个 agent 会失去什么，也没说改主意怎么回来——而这是**不可逆操作前的最后一屏**。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402
import core  # noqa: E402


@pytest.fixture
def wired(monkeypatch, hub_root):
    """把「某 agent 两处都接好了」这件事桩出来——断言不能跟着本机接入状态漂（R719）。

    R754：`hub_root` 同时钉住 core.load_config——没有它，`cli.main(["offboard", …])`
    会去读本机 ~/.agenthub/config.json，CI（无配置）直接退 2，本机却全绿。
    """
    monkeypatch.setattr(core, "find_adapter",
                        lambda root, name: {"agent": "ZCode", "instr_path": "~/AGENTS.md"})
    monkeypatch.setattr(cli, "_boot_of", lambda a: "~/AGENTS.md")
    monkeypatch.setattr(core, "_mcp_spec_of",
                        lambda a: ("~/.zcode/cli/config.json", {"layout": "cli"}))
    monkeypatch.setattr(core, "bootstrap_status", lambda p: True)
    monkeypatch.setattr(core, "bootstrap_version", lambda p: 6)
    monkeypatch.setattr(core, "mcp_has_agenthub", lambda path, layout: True)
    return monkeypatch


def test_preview_lists_targets_without_glued_words(wired, capsys):
    assert cli.main(["offboard", "zcode"]) == 0
    out = capsys.readouterr().out
    assert "移除 引导块" in out and "移除 MCP 条目" in out
    assert "移除MCP" not in out          # 旧版的粘连写法
    assert "移除引导块" not in out       # 也别退回到无分隔


def test_preview_states_impact(wired, capsys):
    assert cli.main(["offboard", "zcode"]) == 0
    out = capsys.readouterr().out
    assert "影响：" in out
    assert "读不到规则" in out and "MCP 调用也会失败" in out
    assert "不受影响" in out


def test_preview_states_rollback(wired, capsys):
    assert cli.main(["offboard", "zcode"]) == 0
    out = capsys.readouterr().out
    assert "回滚：" in out
    assert f"{cli.CMD} onboard zcode" in out     # 改主意的那条命令
    assert ".bak-agenthub-" in out                # 备份长什么样，找得到才能拷回


def test_preview_is_still_a_dry_run(wired, capsys):
    """预演不许有任何写动作：只要没 --apply，remove_* 一次都不该被调用。"""
    called = []
    wired.setattr(core, "remove_bootstrap", lambda *a, **k: called.append("guide") or "")
    wired.setattr(core, "remove_mcp_entry", lambda *a, **k: called.append("mcp") or "")
    assert cli.main(["offboard", "zcode"]) == 0
    assert called == []
