# -*- coding: utf-8 -*-
r"""列表类工具不得「截断不说、把条数当总数」（R267-R269）。

同一族缺陷的三个落点（R260/R265 已修 get_project / list_projects）：
  · hub_list_errors —— error_list 取 100、渲染 [:30]，抬头写「共 {len(errs)}」；
  · hub_list_todos / hub_events / hub_env_list —— 截断无省略号，取满 limit 不提示还有更多。
守：超限抬头报真实总数/给下一步；未超限不出现提示；长文本截断带省略号。
"""
import agenthub_mcp as m
import brain


def _call(name, args, root):
    return m.call_tool(name, args, root)


def test_errors_report_real_total(hub_root):
    brain.init_db(hub_root)
    for i in range(35):
        brain.error_add(hub_root, "qa", f"错误{i}", project="P-项目")
    out = _call("hub_list_errors", {}, hub_root)
    assert "共 35 条错误登记" in out, out[:200]
    assert "这里列 30 条" in out and "status=open/fixed" in out, out[:200]
    out = _call("hub_list_errors", {"limit": 5}, hub_root)
    assert "共 35 条错误登记" in out and "这里列 5 条" in out, out[:200]
    out = _call("hub_list_errors", {"limit": 100}, hub_root)
    assert "这里列" not in out and "（新在前）" in out, out[:200]


def test_errors_status_filter_uses_filtered_total(hub_root):
    brain.init_db(hub_root)
    brain.error_add(hub_root, "qa", "甲", project="P-项目")
    brain.error_add(hub_root, "qa", "乙", project="P-项目")
    first = brain.error_list(hub_root)[0]
    brain.error_set_status(hub_root, first["id"], "fixed")
    out = _call("hub_list_errors", {"status": "open"}, hub_root)
    assert "共 1 条错误登记" in out, out[:200]


def test_todos_truncation_is_visible(hub_root):
    brain.init_db(hub_root)
    with brain.db_conn(hub_root) as conn:
        for i in range(5):
            conn.execute("INSERT INTO records(date,project,agent,title,content,status) "
                         "VALUES(?,?,?,?,?,'active')",
                         ("2026-10-0%d" % (i+1), "P-项目", "qa", f"记录{i}",
                          "后续：跟进第%d项，需要把细节补完整并回报结果" % i))
    out = _call("hub_list_todos", {"limit": 2}, hub_root)
    assert "还有更多" in out and "hub_search" in out, out[:300]
    out = _call("hub_list_todos", {"limit": 50}, hub_root)
    assert "还有更多" not in out, out[:300]


def test_events_truncation_and_ellipsis(hub_root):
    brain.init_db(hub_root)
    for i in range(5):
        brain.event_add(hub_root, "很长的进展内容" * 20, project="P-项目", agent="qa")
    out = _call("hub_events", {"limit": 2}, hub_root)
    assert "还有更早事件" in out and "project/agent" in out, out[:300]
    assert "…" in out, "长内容截断没有省略号"
    out = _call("hub_events", {"limit": 50}, hub_root)
    assert "还有更早事件" not in out


def test_env_list_truncation_and_ellipsis(hub_root):
    brain.init_db(hub_root)
    for i in range(5):
        brain.env_set(hub_root, "系统", f"键{i}", "值" * 100, "qa")
    out = _call("hub_env_list", {"limit": 2}, hub_root)
    assert "还有更多" in out and "category/kw" in out, out[:300]
    assert "…" in out, "长值截断没有省略号"
    out = _call("hub_env_list", {"limit": 50}, hub_root)
    assert "还有更多" not in out
