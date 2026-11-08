# -*- coding: utf-8 -*-
"""R976：被后来者声明修正过的记忆，注入时要带上「以 #N 为准」。

为什么要有这一轮：R974 把体检的词表补全了，但体检是**事后、要人跑**的东西。
真正伤人的场景在送达侧——模型读到 #71「GitHub 凭据全灭」这条旧结论时，
上下文里没有任何一个字告诉它「#370 已经把这条推翻了」。旧结论 + 无标注 = 照旧办事。

边界（一条都不能松）：
  · 只加标注、不改任何数据（裁决是判断，机制不替人做——R887 定的边界）；
  · 只认「声明者 id 更大」的声明：小的修不了大的，否则旧记忆会把新记忆拉下水；
  · 声明者自己必须 live（已归档/已取代的人说的话不算数）；
  · 每条目标最多点名 2 个声明者，多的截掉——注入行只有一行宽；
  · 索引按库文件指纹缓存，命中时 0 次查询（注入路径每句都跑，不能每次全库扫正文）；
  · 任何异常返回空表：标注是锦上添花，不许拖垮召回主路径。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import brain  # noqa: E402


def _mem(root, content, **kw):
    return brain.add_memory(root, content, kind=kw.pop("kind", "fact"),
                            agent="t", **kw)


def test_map_marks_target(hub_root):
    root = str(hub_root)
    brain.init_db(root)
    old = _mem(root, "旧结论：GitHub 强 token 已失效，凭据全灭")
    new = _mem(root, f"修正记忆#{old}：本机有效凭据是 git credential fill")
    m = brain.amended_by_map(root)
    assert m.get(old) == [new]


def test_map_ignores_non_live_declarer(hub_root):
    root = str(hub_root)
    brain.init_db(root)
    old = _mem(root, "旧结论")
    new = _mem(root, f"修正记忆#{old}：新结论")
    brain.set_memory_feedback(root, new, "archive")
    assert old not in brain.amended_by_map(root)


def test_map_ignores_older_declarer(hub_root):
    """老记忆引用新记忆（写错了 id）不算修正——只有新的能修旧的。"""
    root = str(hub_root)
    brain.init_db(root)
    first = _mem(root, "先写的这条提到了后来的某条")
    later = _mem(root, "后写的这条是正解")
    # 手工把「先写的」正文改造成引用 later（模拟 id 写反）
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE memories SET content=? WHERE id=?",
                     (f"修正记忆#{later}：其实我这条才是正解", first))
    assert later not in brain.amended_by_map(root)


def test_map_caps_at_two_declarers(hub_root):
    root = str(hub_root)
    brain.init_db(root)
    old = _mem(root, "一条会被反复修正的旧结论")
    for i in range(3):
        _mem(root, f"修正记忆#{old}：第 {i} 次修正")
    ids = brain.amended_by_map(root).get(old) or []
    assert len(ids) == 2, ids
    assert ids == sorted(ids)


def test_note_render():
    assert brain.amend_note({7: [9]}, 7) == "已被 #9 修正，冲突以它为准；"
    assert brain.amend_note({7: [9, 11]}, 7) == "已被 #9、#11 修正，冲突以它为准；"
    assert brain.amend_note({}, 7) == ""
    assert brain.amend_note({7: [9]}, 8) == ""


def test_map_cached_by_db_fingerprint(hub_root):
    root = str(hub_root)
    brain.init_db(root)
    _mem(root, "甲：旧结论")
    a = brain.amended_by_map(root)
    b = brain.amended_by_map(root)
    assert a is b, "同指纹必须复用同一份索引，不许每次重建"


def test_prompt_recall_text_carries_amend_note(hub_root, tmp_path):
    """端到端：旧结论被注入时，行里必须出现「已被 #N 修正」。"""
    root = str(hub_root)
    brain.init_db(root)
    old = _mem(root, "pyinstaller --exclude-module numpy 打包体积不减，是排除清单写错了")
    new = _mem(root, f"修正记忆#{old}：真正原因是 PYZ 在构建中段固化，"
                     f"--exclude-module 改迟了没用")
    out = brain.prompt_recall(root, "pyinstaller --exclude-module 打包体积不减怎么办",
                              agent="workbuddy", session="s1",
                              state_path=str(tmp_path / "state.json"),
                              update_state=False, origin="eval")
    text = out.get("text") or ""
    ids = out.get("memory_ids") or []
    assert old in ids, (ids, text[:300])
    assert f"已被 #{new} 修正，冲突以它为准" in text, text


def test_selfdoc_does_not_contaminate(hub_root):
    """真库 #561 实锤：说明书里的例句不许被当成裁决。

    这条一旦失守，注入行会写「已被 #13、#561 修正」——#561 是个讲机制的，
    它没修过任何人。
    """
    root = str(hub_root)
    brain.init_db(root)
    old = _mem(root, "旧结论：端口 8080")
    _mem(root, "修正记忆#%d：端口改 9090" % old)
    _mem(root, "【机制说明】举例：正文写「取代记忆#12」就会被审计点名 —— 量具只读",
         tags="机制说明")
    m = brain.amended_by_map(root)
    assert m.get(old) == [old + 1]
    for ids in m.values():
        assert all(i != old + 2 for i in ids), m
