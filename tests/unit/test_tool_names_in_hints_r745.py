# -*- coding: utf-8 -*-
"""R745：digest 的「建议」和 freeze 的「怎么冻结」也在给 MCP 工具名 / 裸 cli.py。

R754：三个用例都加 `hub_root`——此前靠本机 ~/.agenthub/config.json 才活着，CI 上退 2。

真机（真实库 `cli.py digest`）：
      建议：
        - hub_claim 认领；长期无人做就 hub_task_update 关单      ← 命令行照抄 not found
    （建议文案来自 brain.team_digest，MCP 面读它是对的，与 R739 同一个处置）

真机（`cli.py freeze`）：
      写入未冻结。冻结：cli.py freeze --minutes 30 ...          ← 裸 cli.py，少 python
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402
import cli  # noqa: E402


@pytest.fixture(autouse=True)
def _brain(hub_root):
    """R754：digest/freeze 走 _NEED_DB，临时根必须真初始化成大脑，否则根目录闸门退 2。"""
    brain.init_db(hub_root)
    return hub_root


def test_extract_tools_with_word_boundaries():
    assert cli._mcp_tools_in_texts(["hub_claim 认领"]) == ["hub_claim"]
    assert cli._mcp_tools_in_texts(["xhubs_claim 不算"]) == []       # 前面粘着字母不算
    assert cli._mcp_tools_in_texts(["hub_claim 与 hub_next"]) == ["hub_claim", "hub_next"]
    assert cli._mcp_tools_in_texts([]) == []


def _digest(suggestions):
    return {
        "window_days": 1,
        "flow": {"records": 1, "memories": 0, "tasks_created": 0, "tasks_done": 0,
                 "done_cycle_p50_h": None, "tasks_cancelled_or_dead": 0,
                 "errors_opened": 0, "errors_open": 0, "tool_calls": 0,
                 "searches": 0, "zero_hit_searches": 0},
        "current": {"online_agents": 1, "known_agents": 2, "open_total": 0,
                    "wip": {"stale_leases": 0, "unowned": 0, "approval_waiting": 0,
                            "approval_aging": 0, "overdue": 0},
                    "handoffs": {"open": 0, "stale": 0},
                    "security": {"denied": 0},
                    "freeze": {"active": False}},
        "risks": [], "suggestions": suggestions, "agents": [],
    }


def test_digest_adds_cli_hint(monkeypatch, capsys, hub_root):
    monkeypatch.setattr(brain, "team_digest",
                        lambda root, days=1: _digest(["hub_claim 认领；hub_task_update 关单"]))
    assert cli.main(["digest"]) == 0
    out = capsys.readouterr().out
    assert "建议行里的 hub_xxx" in out
    assert f"{cli.CMD} claim" in out and f"{cli.CMD} update" in out


def test_digest_quiet_without_tools(monkeypatch, capsys, hub_root):
    monkeypatch.setattr(brain, "team_digest",
                        lambda root, days=1: _digest(["换关键词/补记录"]))
    assert cli.main(["digest"]) == 0
    assert "MCP 工具名" not in capsys.readouterr().out


def test_freeze_hint_uses_full_prefix(monkeypatch, capsys, hub_root):
    monkeypatch.setattr(brain, "freeze_get", lambda root: {"active": False, "expired": False})
    assert cli.main(["freeze"]) == 0
    out = capsys.readouterr().out
    assert f"{cli.CMD} freeze --minutes 30" in out
    assert "冻结：cli.py freeze" not in out      # 裸前缀不许回来
