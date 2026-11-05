# -*- coding: utf-8 -*-
"""R883：简报优先级与规则簿送达可观测。

两个实锤驱动的改动：
1. 真库简报里「团队最近进展」排在「当前项目/续跑点」之前——团队新闻不是下一步，
   当前项目的干到哪了才是。规则再往下挤，等于开局先读别人的日记。
2. 规则簿只改了文本，流水里看不到送了几条；送达不可复核，就无法回答
   「是不是每家每次开局都真拿到了」。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import brain  # noqa: E402
import brain_brief  # noqa: E402


@pytest.fixture(autouse=True)
def _db(hub_root):
    brain.init_db(hub_root)


def test_当前项目段排在团队进展之前(hub_root):
    brain.add_record(hub_root, "演示-项目", "workbuddy", "2026-11-05",
                     "团队记录", "别人项目的进展")
    brain.add_memory(hub_root, "PROJ-MEM 本项目结论", kind="fact",
                     project="演示-项目", agent="qoder")
    text = brain.session_brief(hub_root, agent="qoder", project="演示-项目")["text"]
    assert text.index("▸ 当前项目") < text.index("▸ 团队最近进展"), text


def test_规则簿送达数进stats(hub_root):
    mid = brain.add_memory(hub_root, "RULE-STATS 统计口径", tags="必做", agent="qoder")
    st = brain.session_brief(hub_root, agent="qoder")["stats"]
    assert st["rules"] == 1
    assert st["rule_ids"] == [mid]
    assert st["rules_total"] == 1


def test_简报钩子流水带规则数(hub_root, capsys):
    brain.add_memory(hub_root, "RULE-J 钩子流水", tags="必做", agent="qoder")
    rc = brain_brief.main(["--root", hub_root, "--agent", "qoder",
                           "--cwd", hub_root, "--json"])
    assert rc == 0
    with brain.db_conn(hub_root) as conn:
        notes = [r["note"] or "" for r in conn.execute(
            "SELECT note FROM journal WHERE action='brain_brief'")]
    assert any("rules=1" in n for n in notes), notes
