# -*- coding: utf-8 -*-
r"""开工对齐视图不得误导（R266）。

`hub_get_progress` 是 agent 开工第一个调用的工具，但：
  ① `title[:60]` 直接把话切断、不留省略号——读起来像错字，「本来就短」与「被截了」分不清；
  ② 每个 agent 只列 10 条，超出部分**静默消失**（分组头不报本段条数）；
  ③ 取满 limit 时不提示还有更早记录——调用方会把「最近 N 条」当全部。
守四件事：长标题带省略号；短标题不加；分组头报本段条数并标明只列 10 条；取满 limit 给出下一步。
"""
import agenthub_mcp as m
import brain


def _seed(root, rows):
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        for date, agent, title in rows:
            conn.execute("INSERT INTO records(date,project,agent,title,content,status) "
                         "VALUES(?,?,?,?,?,'active')",
                         (date, "P-项目", agent, title, "正文"))


def test_long_title_gets_ellipsis(hub_root):
    long_title = "削" * 80
    _seed(hub_root, [("2026-10-28", "qa", long_title)])
    out = m.call_tool("hub_get_progress", {}, hub_root)
    assert ("削" * 60 + "…") in out, out[-200:]
    assert ("削" * 61) not in out


def test_short_title_untouched(hub_root):
    _seed(hub_root, [("2026-10-28", "qa", "短标题")])
    out = m.call_tool("hub_get_progress", {}, hub_root)
    assert "短标题" in out and "短标题…" not in out


def test_group_header_reports_count_and_cap(hub_root):
    _seed(hub_root, [(f"2026-10-{i:02d}", "qa", f"第{i}条") for i in range(1, 15)])
    out = m.call_tool("hub_get_progress", {}, hub_root)
    assert "（本段 14 条，这里列 10 条）" in out, out[:300]
    assert out.count("\n  2026-10-") == 10, "每段应只渲染 10 条"


def test_limit_saturation_points_to_next_step(hub_root):
    _seed(hub_root, [(f"2026-10-{i:02d}", "qa", f"第{i}条") for i in range(1, 8)])
    out = m.call_tool("hub_get_progress", {"limit": 5}, hub_root)
    assert "已取满 limit=5" in out, out[-300:]
    assert "hub_search" in out and "hub_get_record" in out
    out = m.call_tool("hub_get_progress", {"limit": 50}, hub_root)
    assert "已取满" not in out, "没取满时不该提示还有更早记录"
