# -*- coding: utf-8 -*-
r"""工具发现与清单可读性（R277）。

两个真实缺口：
① hub_search_tools 只搜名称+描述——`verify_first`/`keep_zip`/`unread_only` 这类只在
   schema 露脸的能力搜不到，等于不存在；现在参数名也参与匹配并标注命中的参数。
② hub_help / hub_search_tools 用 `description[:56]/[:64]` 硬切，行尾停在半个词上，
   与 R270 起的 `_clip` 约定不一致；现在超长才加「…」。
"""
import agenthub_mcp as m


def test_param_name_search_finds_tool(hub_root):
    out = m.call_tool("hub_search_tools", {"keyword": "verify_first"}, hub_root)
    assert "hub_export" in out and "[参数 verify_first]" in out, out[:400]


def test_param_name_search_keep_zip(hub_root):
    out = m.call_tool("hub_search_tools", {"keyword": "keep_zip"}, hub_root)
    assert "hub_export" in out, out[:400]


def test_description_and_name_search_unchanged(hub_root):
    out = m.call_tool("hub_search_tools", {"keyword": "记忆"}, hub_root)
    assert "hub_memory_write" in out, out[:400]
    out2 = m.call_tool("hub_search_tools", {"keyword": "hub_tasks"}, hub_root)
    assert "hub_tasks" in out2 and "[参数" not in out2, out2[:300]


def test_help_long_description_ends_with_ellipsis(hub_root):
    out = m.call_tool("hub_help", {}, hub_root)
    line = next(l for l in out.splitlines() if l.startswith("- hub_call_tool："))
    assert line.endswith("…"), line


def test_search_long_description_ends_with_ellipsis(hub_root):
    out = m.call_tool("hub_search_tools", {"keyword": "hub_memory_write"}, hub_root)
    line = next(l for l in out.splitlines() if l.startswith("- hub_memory_write"))
    assert line.endswith("…"), line


def test_zero_hit_mentions_param_search(hub_root):
    out = m.call_tool("hub_search_tools", {"keyword": "zzz_no_such_thing"}, hub_root)
    assert "0 命中不一定是没能力" in out and "参数名" in out, out[:400]
