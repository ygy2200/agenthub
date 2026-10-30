# -*- coding: utf-8 -*-
"""反馈报告的人话口径（R795）。

报告是给操作者读的，但修复/建议段落过去直接以 hub_* 工具名开头——相当于把
agent 的命令行当正文。这里钉住三件事：顶部有「hub_* 是什么」的读法、
「值得一试」先给人话再给工具名、摩擦修复都以「让 agent …」开头并保留工具名。
"""
import report


def test_html_doc_explains_tool_names_and_gui_entry():
    d = {"generated": "2026-10-30 12:00",
         "h": {"records": 1, "memories": 2, "searches_total": 3, "honest": {}}}
    html = report._html_doc(d, [("示例", "<p>正文</p>")])
    assert "读法：" in html and "hub_xxx" in html
    assert "agent 可直接执行的工具名" in html
    assert "「运营报告」页点「生成并打开」" in html, "重新生成要给人一条界面入口"


def test_try_section_puts_description_before_tool_name():
    html = report._sec_try({"tool_used": [], "done_todos": 0})
    assert "检查工具箱" in html and "hub_ops_run" in html
    assert html.index("检查工具箱") < html.index("hub_ops_run"), \
        "应先说能力再给工具名"
    assert "hub_ops_run</code>）" in html


def test_friction_fixes_lead_with_human_action():
    d = {"h": {"errors_open": 2, "dup_memory_count": 0, "memories": 0,
               "projects_stalled": 1},
         "zero_mem": 0, "meta_breakdown": {}, "meta_polluted": False}
    fixes = {f["title"]: f["fix"] for f in report._frictions(d)}
    assert len(fixes) == 2, fixes
    for title, fix in fixes.items():
        assert fix.startswith("让 agent"), f"{title} 的修复仍以工具名打头：{fix}"
        assert "hub_" in fix, f"{title} 的修复应保留可转给 agent 的工具名"
