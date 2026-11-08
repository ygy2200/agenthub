# -*- coding: utf-8 -*-
"""R975：写记忆时，正文提到 #N 却没标取代——当场提示，不等事后体检。

为什么要有这一轮：R974 把体检的词表扩开了，但那是**事后**才发现。真库
#370「修正记忆#71」、#497「修正记忆#495」这两条写下去的那一刻，谁都没提醒
「你提到了 #71，但它还是现行条目」——所以 #71「GitHub 凭据全灭」、#495
「DLSS 菜单消失=mod 与 exe 失配」这两个已被推翻的结论又活了半个月。

设计边界（别把提示做成噪音）：
  · 只提示、不阻断写入（写入是事实，裁决是判断，机制只给出口）；
  · 目标已被取代 / 本次已 supersedes 到它 → 一个字都不说；
  · 最多点名 3 条，多的只说「还有 N 条」；
  · 「补充记忆#N」不算裁决意图（追加不是推翻），不提示；
  · 动词表与 ops 体检共用 brain.DECL_VERBS，单一事实源（R974 的教训：
    两张表各写一遍就会漂）。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import brain  # noqa: E402


@pytest.fixture
def hub(tmp_path):
    root = tmp_path / "hub"
    root.mkdir()          # init_db 对不存在的根目录只返回字符串不建库，必须先建
    err = brain.init_db(str(root))
    assert not err, err
    return str(root)


def _mem(root, content, **kw):
    return brain.add_memory(root, content, kind=kw.pop("kind", "fact"),
                            agent="t", **kw)


# ---------- 纯函数：声明的解析 ----------

def test_refs_basic():
    assert brain.declaration_refs("修正记忆#12 的结论") == [("修正", 12)]
    assert brain.declaration_refs("取代 #5：端口改了") == [("取代", 5)]


def test_refs_date_is_not_id():
    """真库 #49 实锤：年份不许被当成记忆 id。"""
    assert brain.declaration_refs("修正 2026-10-01 那次事故的结论") == []


def test_refs_priority_dedup():
    """同一 id 被多种动词提到时只算一次，动词取优先级高的。"""
    refs = brain.declaration_refs("取代记忆#3；顺带修正记忆#3 的措辞")
    assert refs == [("取代", 3)]


def test_refs_supplement_is_not_a_ruling():
    assert brain.declaration_refs("补充记忆#7：还有一个细节") == []


def test_refs_empty():
    assert brain.declaration_refs("") == []
    assert brain.declaration_refs(None) == []


# ---------- 写后提示 ----------

def test_hint_when_target_still_live(hub):
    old = _mem(hub, "旧结论：GitHub 凭据全灭")
    body = f"修正记忆#{old}：有效凭据是 git credential fill"
    new = _mem(hub, body)
    h = brain.declaration_hint(hub, new, body)
    assert f"#{old}" in h
    assert "hub_memory_supersede" in h or "supersedes" in h


def test_hint_silent_when_already_superseded(hub):
    old = _mem(hub, "旧结论")
    _mem(hub, f"取代记忆#{old}：新结论", supersedes=old)
    new = _mem(hub, f"修正记忆#{old}：再确认一遍")
    assert brain.declaration_hint(hub, new, f"修正记忆#{old}：再确认一遍") == ""


def test_hint_silent_when_supersedes_covers_it(hub):
    old = _mem(hub, "旧结论 A")
    new = _mem(hub, f"取代记忆#{old}：新结论 A", supersedes=old)
    assert brain.declaration_hint(hub, new, f"取代记忆#{old}：新结论 A",
                                  supersedes=old) == ""


def test_hint_names_missing_target(hub):
    new = _mem(hub, "修正记忆#99999：笔误")
    h = brain.declaration_hint(hub, new, "修正记忆#99999：笔误")
    assert "#99999" in h
    assert "不存在" in h


def test_hint_silent_without_declaration(hub):
    new = _mem(hub, "一条普通事实，没有引用任何旧记忆")
    assert brain.declaration_hint(hub, new, "一条普通事实，没有引用任何旧记忆") == ""


def test_hint_caps_at_three(hub):
    olds = [_mem(hub, f"旧结论 {i}") for i in range(5)]
    body = "；".join(f"修正记忆#{o}" for o in olds)
    new = _mem(hub, body)
    h = brain.declaration_hint(hub, new, body)
    assert "另有 2 条未列出" in h


# ---------- 接线：hub_memory_write 的返回里真的带上这句 ----------

def test_mcp_memory_write_carries_hint(hub):
    import agenthub_mcp
    old = _mem(hub, "旧结论：桌面跑的是旧 GUI")
    out = agenthub_mcp.call_tool("hub_memory_write",
                                 {"content": f"修正记忆#{old}：exe 已重打包",
                                  "kind": "lesson"}, hub)
    assert "已写入大脑记忆" in out
    assert f"#{old}" in out, out
