# -*- coding: utf-8 -*-
"""R886 / B092：取代声明未裁决量具的边界。

先写会红的用例再落地工具（本仓协议）。核心不是「能不能扫出声明」，
而是**别把不该作废的声明当成裁决依据**：真库 live 目标里至少有 6 条
是局部取代 / 补充而非取代 / 续写仍有效，纯文本正则连否定句都分不出。
所以量具必须：只读、只点名、给分类提示、不写库、不自动取代。

R886 首跑真实红了两处，都留成回归钉子：
  ① 夹具只插 #1 却声明指向 #34/#92/… → 全被判成「目标不存在」；
  ② 真库实跑发现机制说明书（记忆 #561）引用的例句被当成 4 条假声明
     （B040 自指污染同族）→ 量具必须跳过自述/机制说明并单列计数。
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location(
    "supersede_declaration_audit", ROOT / "tools" / "supersede_declaration_audit.py")
audit_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_mod)


@pytest.fixture
def db(tmp_path):
    """最小 memories 表：量具只依赖 id/content/superseded_by/status/tags 五列。"""
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


def test_live_target_is_named_but_not_judged(db):
    _add(db, 1, "旧结论：端口 8080")
    _add(db, 2, "取代记忆#1：端口已改 9090")
    r = audit_mod.audit(db)
    assert len(r["pending"]) == 1
    assert r["pending"][0]["target"] == 1
    assert r["pending"][0]["class"] == "未见限定词"
    assert r["by_class"]["未见限定词"] == 1
    assert r["distinct_live_targets"] == [1]
    assert "不是裁决" in r["note"]


@pytest.mark.parametrize("target_id,text,expected", [
    (1, "取代记忆#1中「TLS 错误归因」的修法", "局部取代"),
    (34, "取代记忆#34第③条：Claude Code 侧已修复", "局部取代"),
    (92, "【坑·取代记忆#92 的修法部分】env 不可靠", "局部取代"),
    (49, "部分取代记忆#49：secret 已不为空", "局部取代"),
    (67, "取代记忆#67（其内容仍有效，本条续写进展）", "续写仍有效"),
    (252, "【补充而非取代记忆#252】推进了一步", "补充而非取代"),
])
def test_qualified_declarations_are_classified_not_dropped(db, target_id, text, expected):
    """六类真库原句：都要被点名（不能静默丢），但必须带对分类提示。

    首跑这里全红，因为夹具只插了 #1——声明指向 #34/#92/… 全成了 dangling。
    修法：按声明里的真实目标 id 插目标，声明者固定用 900。
    """
    _add(db, target_id, "旧结论")
    _add(db, 900, text)
    r = audit_mod.audit(db)
    assert len(r["pending"]) == 1, "限定声明也要可见，静默丢弃等于重新变聋"
    assert r["pending"][0]["target"] == target_id
    assert r["pending"][0]["class"] == expected, (text, r["pending"][0])


def test_supplement_not_supersede_is_flagged_as_negative(db):
    """#400 明写「补充而非取代」却照样命中正则——分类必须把否定标出来。"""
    _add(db, 1, "onefile 冒烟坑")
    _add(db, 2, "【补充而非取代记忆#1】选到带句柄的子进程也不等于界面就绪")
    r = audit_mod.audit(db)
    assert r["by_class"]["补充而非取代"] == 1
    assert r["by_class"]["未见限定词"] == 0


def test_selfdoc_memory_is_excluded_not_counted(db):
    """真库 #561 实锤：机制说明书里引用的例句不算声明（B040 自指污染同族）。"""
    _add(db, 1, "旧结论")
    _add(db, 2, "【取代声明 ≠ 结构化取代】举例：#13 取代 #12 中错误归因")
    r = audit_mod.audit(db)
    assert r["skipped_selfdoc"] == [2]
    assert r["pending"] == []
    assert r["declarations"] == 0


def test_mechanism_tag_also_excludes(db):
    """标签通道：写的人按 B040 标了「机制说明」就走结构化排除，不靠正文猜。"""
    _add(db, 1, "旧结论")
    _add(db, 2, "取代记忆#1：讲机制时举的例子", tags="机制说明,B092")
    r = audit_mod.audit(db)
    assert r["skipped_selfdoc"] == [2] and r["pending"] == []


def test_resolved_target_is_not_named_again(db):
    """已由结构化链路裁决的不重复点名（stale_health 硬不变量的下游口径）。"""
    _add(db, 1, "旧结论", sup=2)
    _add(db, 2, "取代记忆#1：新结论")
    r = audit_mod.audit(db)
    assert r["pending"] == []
    assert len(r["resolved"]) == 1


def test_retired_target_is_not_named(db):
    _add(db, 1, "旧结论", status="retired")
    _add(db, 2, "取代记忆#1：新结论")
    assert audit_mod.audit(db)["pending"] == []


def test_missing_target_is_dangling_not_guessed(db):
    """目标不存在：不猜、不建、不塞进待判定。"""
    _add(db, 9, "取代记忆#777：某条不存在的旧记忆")
    r = audit_mod.audit(db)
    assert r["pending"] == []
    assert len(r["dangling"]) == 1 and r["dangling"][0]["target"] == 777


def test_no_declaration_reports_empty(db):
    _add(db, 1, "普通记忆，没有取代声明")
    r = audit_mod.audit(db)
    assert r["declarations"] == 0 and r["pending"] == [] and r["by_class"]["未见限定词"] == 0


def test_audit_never_writes(db):
    """最硬的一条：量具是只读的，跑完库里一行都不许变。"""
    _add(db, 1, "旧结论")
    _add(db, 2, "取代记忆#1：新结论")
    con = sqlite3.connect(db)
    before = con.execute("SELECT * FROM memories ORDER BY id").fetchall()
    con.close()
    audit_mod.audit(db)
    con = sqlite3.connect(db)
    after = con.execute("SELECT * FROM memories ORDER BY id").fetchall()
    con.close()
    assert after == before, "量具写库了——自动裁决是被禁止的"
