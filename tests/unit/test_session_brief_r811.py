# -*- coding: utf-8 -*-
"""会话简报 R811：开工无需提醒就能看到的上下文快照。

病根：引导块只写「开工先 hub_get_progress」——这是建议，模型可以不理；真实库
doctor 显示 claude/dsh/hermes 配置了 MCP 却近 7 天零调用。修法不是再写一句
更重的建议，而是把上下文**直接送到会话里**：SessionStart 钩子输出简报。
本文件钉住简报的三条底线：该有的段落都在、项目推断不瞎猜、永远不超预算。
"""
import agenthub_mcp as m
import brain


def _seed(root):
    brain.init_db(root)
    brain.add_record(root, "演示-会话简报", "qoder", "2026-10-30",
                     "2026-10-30（qoder）· 做完了简报主体",
                     "目的：验证简报\n做了什么：加 session_brief\n验证结果：单测\n如何回滚：删函数")
    brain.add_record(root, "另一个项目", "workbuddy", "2026-10-29",
                     "2026-10-29（workbuddy）· 别的项目在动", "内容")
    brain.add_memory(root, "简报必须带项目相关记忆", kind="lesson",
                     project="演示-会话简报", agent="qoder", pinned=True)
    brain.add_memory(root, "无关项目记忆不该乱入", kind="fact", project="无关项目")
    brain.signal_send(root, "workbuddy", "并发提醒：同一工作树有另一个会话",
                      to_agent="qoder")
    brain.task_add(root, "把简报接进钩子", project="演示-会话简报", priority=0)


def test_brief_carries_the_context_an_agent_needs(hub_root):
    _seed(hub_root)
    res = brain.session_brief(hub_root, agent="qoder",
                              cwd=f"{hub_root}/演示-会话简报")
    t = res["text"]
    assert res["project"] == "演示-会话简报", res
    for needle in ("团队最近进展", "当前项目：演示-会话简报", "做完了简报主体",
                   "简报必须带项目相关记忆", "置顶记忆", "未读信号 1 条",
                   "并发提醒", "可认领 1 条", "收工约定"):
        assert needle in t, f"简报缺「{needle}」：\n{t}"
    assert "无关项目记忆不该乱入" not in t
    assert res["elapsed_ms"] >= 0


def test_project_inference_only_trusts_registered_projects(hub_root):
    _seed(hub_root)
    assert brain.infer_project_from_cwd(hub_root, f"{hub_root}/演示-会话简报/子目录") == "演示-会话简报"
    assert brain.infer_project_from_cwd(hub_root, hub_root) == ""
    assert brain.infer_project_from_cwd(hub_root, f"{hub_root}/没登记过的目录") == ""
    assert brain.infer_project_from_cwd(hub_root, "C:/Windows") == ""


def test_brief_never_exceeds_budget(hub_root):
    brain.init_db(hub_root)
    for i in range(12):
        brain.add_memory(hub_root, f"超长记忆 {i}：" + "细节" * 200, kind="lesson", pinned=True)
    res = brain.session_brief(hub_root, agent="qoder", max_chars=420)
    assert res["truncated"] is True
    assert len(res["text"]) <= 420, len(res["text"])
    assert res["text"].endswith("已截断）")


def test_invalid_root_fails_open(hub_root):
    res = brain.session_brief("", agent="qoder")
    assert res["text"] == "" and res["elapsed_ms"] == 0
    res = brain.session_brief(f"{hub_root}/不存在", agent="qoder")
    assert res["text"] == ""


def test_mcp_tool_and_cli_share_the_same_brief(hub_root):
    _seed(hub_root)
    out = m.call_tool("hub_brief", {"cwd": f"{hub_root}/演示-会话简报"}, hub_root)
    assert "团队最近进展" in out and "当前项目：演示-会话简报" in out, out[:300]
