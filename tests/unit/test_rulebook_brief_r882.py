# -*- coding: utf-8 -*-
"""R882：共享规则簿——用户级硬规则随每次开工简报自动送达，不靠检索碰运气。

实锤：30 天注入 638 次、仅 40 次被用（6.3%），`tags=必做` 真库 0 条。
机制（R879 档位/R880 声明）都在，但「所有 agent 每次开工都能看到主人红线」
这件事只靠 pinned 三选一，规则会被项目近况/团队进展挤掉或截断。
本轮把 tags=必做 的规则单独成段、放在简报最前，并精确匹配标签，防止
「必做补充说明」这类近义词混进来制造噪声。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import brain  # noqa: E402


@pytest.fixture(autouse=True)
def _db(hub_root):
    brain.init_db(hub_root)


def _add(root, content, tags="必做", kind="note", pinned=False, supersedes=0):
    return brain.add_memory(root, content, kind=kind, tags=tags,
                            agent="qoder", pinned=pinned, supersedes=supersedes)


def _brief(root):
    return brain.session_brief(root, agent="qoder", cwd=root)["text"]


def test_规则段随简报常驻(hub_root):
    _add(hub_root, "【硬规则】不要在回复末尾贴闸门快照")
    text = _brief(hub_root)
    assert "▸ 必做规则" in text
    assert "不要在回复末尾贴闸门快照" in text


def test_只认精确标签_近义词与机制说明不混入(hub_root):
    _add(hub_root, "RULE-EXACT 精确标签")
    _add(hub_root, "RULE-NEAR 必做补充说明", tags="必做补充说明")
    _add(hub_root, "RULE-SELFDOC 机制说明", tags="机制说明,必做")
    text = _brief(hub_root)
    assert "RULE-EXACT" in text
    assert "RULE-NEAR" not in text
    assert "RULE-SELFDOC" not in text, "讲机制本身的记忆不是硬规则，不能占规则段"


def test_无必做规则时不出空段(hub_root):
    _add(hub_root, "普通观察", tags="观察")
    text = _brief(hub_root)
    assert "▸ 必做规则" not in text


def test_规则段最多八条且给出总数出口(hub_root):
    for i in range(10):
        _add(hub_root, f"RULE-{i} 第{i}条")
    text = _brief(hub_root)
    assert text.count("RULE-") == 8, text
    assert "已列 8/10" in text
    assert "hub_memory_read" in text


def test_被取代的规则不再出现在规则段(hub_root):
    old = _add(hub_root, "RULE-OLD 旧规则")
    _add(hub_root, "RULE-NEW 新规则", supersedes=old)
    text = _brief(hub_root)
    assert "RULE-NEW" in text
    assert "RULE-OLD" not in text


def test_规则行在句读处收尾_不切半标记(hub_root):
    _add(hub_root, "规则第一句" * 18 + "。第二句" + "细节" * 120)
    text = _brief(hub_root)
    line = next(l for l in text.splitlines() if l.startswith("- #"))
    assert "第二句" not in line and "细节" not in line, line
    assert line.endswith("。…"), line


def test_规则段排在团队进展与置顶记忆之前(hub_root):
    _add(hub_root, "RULE-FIRST 先看规则")
    _add(hub_root, "PINNED-BACKGROUND 背景", tags="观察", pinned=True)
    text = _brief(hub_root)
    i_rule = text.index("▸ 必做规则")
    i_pin = text.find("▸ 置顶记忆")
    assert i_pin == -1 or i_rule < i_pin
    assert text.index("RULE-FIRST") < (i_pin if i_pin != -1 else len(text))
