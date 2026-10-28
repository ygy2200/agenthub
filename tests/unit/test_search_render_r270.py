# -*- coding: utf-8 -*-
r"""检索结果不低估、不切断（R270）。

`search_all` 每类最多 30 条，但 MCP 抬头把 len() 写成「命中 N」——命中 100 条记录
也只显示 30，调用方会以为只有 30 条。且标题/记忆/任务/错误一律 `[:N]` 无省略号。
守四件事：取满上限的类别标 + 并说明是上限；未取满不标 +；长标题带省略号；
_clip 纯函数边界（短串不动、长串留省略号）。
"""
import agenthub_mcp as m
import brain


def _rec(i, title=None):
    return {"id": i, "date": "2026-10-01", "project": "P-项目", "agent": "qa",
            "title": title or f"备份方案第{i}版", "content": f"备份正文 {i}"}


def _fake_search(records):
    def fake(root, kw, limit=30):
        return {"records": records, "memories": [], "notes": [], "tasks": [],
                "errors": [], "files": []}
    return fake


def _with_search(fake, fn):
    old = brain.search_all
    brain.search_all = fake
    try:
        fn()
    finally:
        brain.search_all = old


def test_saturated_category_marked(hub_root):
    def run():
        out = m.call_tool("hub_search", {"keyword": "备份"}, hub_root)
        assert "记录30+" in out, out[:300]
        assert "已取满每类 30 条上限" in out, out[:300]
    _with_search(_fake_search([_rec(i) for i in range(30)]), run)


def test_unsaturated_category_has_no_plus(hub_root):
    def run():
        out = m.call_tool("hub_search", {"keyword": "备份"}, hub_root)
        assert "记录3 ·" in out, out[:300]
        assert "记录3+" not in out
        assert "已取满" not in out
    _with_search(_fake_search([_rec(i) for i in range(3)]), run)


def test_long_title_gets_ellipsis(hub_root):
    def run():
        out = m.call_tool("hub_search", {"keyword": "备份"}, hub_root)
        assert "…" in out, out[:400]
        assert ("长" * 61) not in out
    _with_search(_fake_search([_rec(1, "备份" + "长" * 80)]), run)


def test_clip_helper():
    assert m._clip("abc", 5) == "abc"
    assert m._clip("abcdef", 5) == "abcde…"
    assert m._clip(None, 5) == ""
    assert m._clip("abcde", 5) == "abcde"
