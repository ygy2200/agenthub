# -*- coding: utf-8 -*-
r"""项目列表不得把「取到的条数」当总数（R265）。

`list_projects` 一直 LIMIT 50，MCP 渲染写「共 {len(rows)} 个项目（…前 50）」——
真实库正好 50 个项目所以没暴露；第 51 个项目出现时，调用方会看到「共 50 个」
而永远不知道还有更多（R260 在 get_project 上修的同型缺陷，换了个位置）。
守四件事：超过上限时抬头报真实总数并提示截断；limit 可调（1–200）；
未超限时不出现截断提示；旧的硬编码「前 50」不再出现。
"""
import agenthub_mcp as m
import brain


def _seed(root, n):
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        for i in range(n):
            conn.execute("INSERT INTO projects(name,agent,created,status) VALUES(?,?,?,?)",
                         (f"P{i:02d}", "qa", "2026-10-01", "active"))


def test_over_limit_reports_total_and_truncation(hub_root):
    _seed(hub_root, 60)
    out = m.call_tool("hub_list_projects", {}, hub_root)
    assert "共 60 个项目" in out, out[:200]
    assert "只列最近 50" in out, out[:200]
    assert "前 50" not in out, "旧的硬编码截断文案还在"


def test_limit_is_adjustable(hub_root):
    _seed(hub_root, 60)
    out = m.call_tool("hub_list_projects", {"limit": 3}, hub_root)
    assert "共 60 个项目" in out and "只列最近 3" in out, out[:200]
    out = m.call_tool("hub_list_projects", {"limit": 999}, hub_root)
    assert "只列最近" not in out, "到达上限后不该再提示截断"
    assert out.count("- P") == 60


def test_under_limit_has_no_truncation_hint(hub_root):
    _seed(hub_root, 5)
    out = m.call_tool("hub_list_projects", {}, hub_root)
    assert "共 5 个项目（按最近活动排序）" in out, out[:200]
    assert "只列最近" not in out


def test_bad_limit_is_rejected_not_silently_defaulted(hub_root):
    """坏类型走集中校验：一个错类型不该被悄悄换成默认值（Round13 的教训）。"""
    _seed(hub_root, 3)

    def call(args):
        resp = m.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "hub_list_projects", "arguments": args}},
                                hub_root)
        return resp["result"]

    res = call({"limit": "abc"})
    assert res["isError"] is True and "必须是整数" in res["content"][0]["text"], res
    res = call({"limit": 0})
    assert res["isError"] is False and "共 3 个项目" in res["content"][0]["text"], res
