# -*- coding: utf-8 -*-
"""R974：声明动词表只认「取代」，把「修正/更正」这一整类漏在审计之外。

为什么这是真缺口（不是造需求）：
  外部三份一手材料都把 stale fact 列为记忆系统第一失效模式（旧结论被新结论推翻后
  仍在推送）。本仓 R887 已有只读点名，但 `decl_re` 只匹配「取代(记忆)#N」——
  真库实测量出来的 7 条「修正/更正记忆#N」全不在点名范围里，其中
  #370→#71（GitHub 凭据「全灭」已被推翻）、#497→#495（DLSS 菜单消失的真主因已被更正）
  的前条至今 live，会被检索和简报照常推送。

边界（R887 定过，这里继续遵守，别在测试里松掉）：
  ① 只点名不裁决，pending 是「声明对」不是判决；
  ② 「补充记忆#N」不是裁决对象，单列计数、不进 pending；
  ③ 数字后面紧跟 `-` 或数字时不是记忆 id（真库 #49「修正 2026-10-01」会被解析成 2026）；
  ④ 机制说明书（tags=机制说明）自指引用照旧跳过。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import ops  # noqa: E402


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "brain.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, "
                "superseded_by INTEGER DEFAULT 0, status TEXT DEFAULT 'active', "
                "tags TEXT DEFAULT '')")
    con.commit()
    con.close()
    return p


def _add(db, mid, content, sup=0, status="active", tags=""):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO memories (id, content, superseded_by, status, tags) "
                "VALUES (?, ?, ?, ?, ?)", (mid, content, sup, status, tags))
    con.commit()
    con.close()


def test_fix_verb_is_named(db):
    """「修正记忆#1」必须点名——这是 R974 的主诉。"""
    _add(db, 1, "旧结论：GitHub 凭据全灭")
    _add(db, 2, "修正记忆#1：本机有效凭据是 git credential fill")
    r = ops.supersede_declaration_audit(db)
    assert r["distinct_live_targets"] == [1]
    assert r["pending"][0]["verb"] == "修正"


def test_correct_verb_is_named(db):
    _add(db, 1, "旧结论：P0 要加分支删掉 csgo 缓存")
    _add(db, 2, "更正记忆#1 第①条：方向错了一半")
    r = ops.supersede_declaration_audit(db)
    assert r["distinct_live_targets"] == [1]
    assert r["pending"][0]["verb"] == "更正"


def test_supplement_is_counted_not_pending(db):
    """补充不是裁决对象：不进 pending，但要能被数到（否则读者以为漏扫）。"""
    _add(db, 1, "某条事实")
    _add(db, 2, "补充记忆#1：还有一个细节")
    r = ops.supersede_declaration_audit(db)
    assert r["pending"] == []
    assert r["supplement_declared"] == 1
    assert r["distinct_live_targets"] == []


def test_date_after_verb_is_not_an_id(db):
    """真库 #49 实锤：'修正 2026-10-01 的记录' 不许解析成记忆 #2026。"""
    _add(db, 1, "某条事实")
    _add(db, 2, "修正 2026-10-01 那次事故的结论：根因不在编码")
    r = ops.supersede_declaration_audit(db)
    assert r["dangling"] == []
    assert r["distinct_live_targets"] == []


def test_same_pair_dedup_keeps_strongest_verb(db):
    """同一对既写「取代」又写「修正」时只算一次，动词取优先级最高的。"""
    _add(db, 1, "旧结论")
    _add(db, 2, "取代记忆#1；顺带修正记忆#1 的措辞")
    r = ops.supersede_declaration_audit(db)
    assert len(r["pending"]) == 1
    assert r["pending"][0]["verb"] == "取代"


def test_by_verb_sums_to_pending(db):
    _add(db, 1, "旧 A")
    _add(db, 2, "旧 B")
    _add(db, 3, "取代记忆#1：A 已改")
    _add(db, 4, "更正记忆#2：B 的数字错了")
    _add(db, 5, "修正记忆#2 的结论：B 其实相反")
    r = ops.supersede_declaration_audit(db)
    assert sum(r["by_verb"].values()) == len(r["pending"])
    assert r["by_verb"]["取代"] == 1
    assert r["by_verb"]["更正"] == 1
    assert r["by_verb"]["修正"] == 1


def test_selfdoc_still_skipped(db):
    _add(db, 1, "某条事实")
    _add(db, 2, "修正记忆#1 的例句：这是机制说明", tags="机制说明")
    r = ops.supersede_declaration_audit(db)
    assert r["pending"] == []
    assert 2 in r["skipped_selfdoc"]


def test_resolved_target_not_pending(db):
    _add(db, 1, "旧结论", sup=2)
    _add(db, 2, "修正记忆#1：新结论")
    r = ops.supersede_declaration_audit(db)
    assert r["pending"] == []
    assert len(r["resolved"]) == 1
