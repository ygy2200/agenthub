# -*- coding: utf-8 -*-
r"""R1021：检索结果分账 + 弱命中折叠——别把一堆弱相关当成答案倒给模型。

真库实锤（2026-11-08）：查「量子纠缠 卷积 蒸馏」（库里根本没有这些内容），
旧行为返回 记录32 + 记忆30 = 62 条，其中 54 条是弱相关（多关键词没同时命中），
模型会把它们当成真答案读进去。真正的关键词命中是 **0 条**。
更隐蔽的一处：图关联行（_cross，沿知识图谱带回来的）没有弱标记，
被旧账本算成「强命中 8」——于是一次零命中的查询看起来像命中了 8 条。

本文件锁：
1. 三分类记账：关键词命中 / 图关联 / 弱相关，计数互不混淆；
2. 弱相关只留 3 条备查，其余在抬头明说没列（不许悄悄丢）；
3. 某一类「无一关键词命中」时必须明说——这是「你这条查询基本没查到」的关键信号；
4. 真查询不受影响：关键词命中的结果照旧全列。

渲染层测的是「怎么把结果摆给模型看」，所以用夹具喂定稿的分类结果，
不去赌检索内部（近重复折叠等）的具体行为。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402
import agenthub_mcp  # noqa: E402


def _rec(i, **kw):
    r = {"id": i, "date": "2026-11-08", "project": "P1", "agent": "qoder",
         "title": f"记录 {i}", "content": "正文"}
    r.update(kw)
    return r


def _mem(i, **kw):
    r = {"id": i, "kind": "lesson", "content": "记忆正文", "tags": ""}
    r.update(kw)
    return r


@pytest.fixture
def patched_search(monkeypatch):
    def _patch(rows, mems):
        def _fake(root, kw, limit=30):
            return {"records": rows, "memories": mems, "notes": [], "tasks": [],
                    "errors": [], "files": []}
        monkeypatch.setattr(brain, "search_all", _fake)
    return _patch


def _search(root, kw="查询"):
    return agenthub_mcp.call_tool("hub_search", {"keyword": kw}, root)


def test_header_splits_three_classes(tmp_path, patched_search):
    rows = [_rec(1)] + [_rec(10 + i, _cross="图关联自memory") for i in range(2)] \
        + [_rec(20 + i, _weak=True) for i in range(8)]
    patched_search(rows, [_mem(50 + i, _weak=True) for i in range(5)])
    head = _search(str(tmp_path)).splitlines()[0]
    assert "关键词1" in head, head
    assert "图关联2" in head, head
    assert "弱8" in head, head


def test_weak_rows_capped_and_counted(tmp_path, patched_search):
    rows = [_rec(1)] + [_rec(20 + i, _weak=True) for i in range(8)]
    patched_search(rows, [])
    out = _search(str(tmp_path))
    lines = out.splitlines()
    assert len([l for l in lines if l.startswith("[记录#")]) == 4, "1 命中 + 3 弱相关备查"
    assert any("未列" in l for l in lines), "少列了弱相关就必须说清楚少了多少"


def test_no_keyword_hit_is_declared(tmp_path, patched_search):
    """全类都没有关键词命中时必须明说——这是「你这条查询基本没查到」的关键信号。"""
    rows = [_rec(20 + i, _weak=True) for i in range(4)]
    patched_search(rows, [_mem(50 + i, _weak=True) for i in range(3)])
    out = _search(str(tmp_path))
    assert "无一关键词命中" in out, out[:500]
    assert len([l for l in out.splitlines() if l.startswith("[记忆#")]) == 3


def test_keyword_hits_are_not_capped(tmp_path, patched_search):
    """关键词命中的结果照旧全列（折叠只针对弱相关与图关联备查）。"""
    rows = [_rec(i) for i in range(1, 7)]
    patched_search(rows, [])
    out = _search(str(tmp_path))
    assert len([l for l in out.splitlines() if l.startswith("[记录#")]) == 6


def test_split_helper_classifies():
    rows = [{"_weak": True}, {"_cross": True}, {}, {"_cross": True, "_weak": True}]
    hit, cross, weak = agenthub_mcp._search_split(rows)
    assert len(hit) == 1 and len(cross) == 1 and len(weak) == 2
