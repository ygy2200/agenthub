# -*- coding: utf-8 -*-
"""R884：其他 agent 最近 7 天的踩坑自动进简报——共同记忆不能等自己再踩一遍。

边界：自己的经验不用再推（自己刚干过）、旧坑不占版面、机制说明不冒充踩坑、
最多两条、与规则段/项目记忆去重。
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import brain  # noqa: E402


@pytest.fixture(autouse=True)
def _db(hub_root):
    brain.init_db(hub_root)


def _lesson(root, content, agent="workbuddy", days_old=0, tags="agenthub"):
    mid = brain.add_memory(root, content, kind="lesson", tags=tags, agent=agent)
    if days_old:
        ts = (datetime.datetime.now() - datetime.timedelta(days=days_old)
              ).isoformat(timespec="seconds")
        with brain.db_conn(root) as conn:
            conn.execute("UPDATE memories SET updated=? WHERE id=?", (ts, mid))
    return mid


def _brief(root, agent="qoder"):
    return brain.session_brief(root, agent=agent, cwd=root)


def test_其他agent最近的踩坑自动进简报(hub_root):
    _lesson(hub_root, "LESSON-OTHER 别人踩过的坑")
    text = _brief(hub_root)["text"]
    assert "▸ 最近团队踩坑" in text
    assert "LESSON-OTHER" in text


def test_自己的旧的与机制说明都不进(hub_root):
    _lesson(hub_root, "LESSON-SELF 自己的经验", agent="qoder")
    _lesson(hub_root, "LESSON-OLD 三十天前", days_old=30)
    _lesson(hub_root, "LESSON-MECH 机制说明", tags="机制说明")
    text = _brief(hub_root)["text"]
    assert "LESSON-SELF" not in text
    assert "LESSON-OLD" not in text
    assert "LESSON-MECH" not in text


def test_最多两条(hub_root):
    _lesson(hub_root, "LESSON-1 第一条")
    _lesson(hub_root, "LESSON-2 第二条")
    _lesson(hub_root, "LESSON-3 第三条")
    res = _brief(hub_root)
    assert res["stats"]["lessons"] == 2
    assert "LESSON-3" in res["text"] and "LESSON-2" in res["text"]
    assert "LESSON-1" not in res["text"]


def test_已是规则的踩坑不重复出现(hub_root):
    _lesson(hub_root, "LESSON-RULE 同时是硬规则", tags="必做")
    text = _brief(hub_root)["text"]
    assert text.count("LESSON-RULE") == 1, text
