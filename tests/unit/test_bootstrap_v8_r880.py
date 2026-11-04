# -*- coding:utf-8 -*-
"""R880 · B089 配套：让「必做」这枚声明通道进入各家共同的引导块。

R879 给注入加了策略层（required/supportive 分档），但 `tags=必做/指令` 这条
写入分支在真库是 **0 条**——引导块不提、MCP 工具描述不提，写记忆的 agent
根本不知道可以声明「以后遇到 X 必须照做」。机制只有代码知道 = 等于没有。

同样照 R858 的教训：光写义务不够，要写清回报（写了会被标【必须照做】、
动手前先落实），并且动作与收益必须挨着说；还要写明「普通观察别乱标」，
否则 528 条记忆全标必做，等于把噪声整体升格成硬约束。

每条都先在 v7 引导块上红。
"""
import core
import agenthub_mcp as m

OLD_BLOCK = (
    f"{core.BOOTSTRAP_BEGIN}\n"
    "<!-- agenthub:version:7 -->\n"
    "## AgentHub 公用大脑（自动接入，请勿删除本标记块）\n"
    "- 有跨会话价值的知识 hub_memory_write(content, kind, tags)\n"
    f"{core.BOOTSTRAP_END}\n"
)


def test_bootstrap_version_bumped_to_v8():
    """引导内容变了版本必须 +1，否则 boot_stale 与 refresh 看不见这次改动。"""
    assert core.BOOTSTRAP_VERSION == "8"


def test_bootstrap_teaches_directive_tag_with_payoff():
    t = core.bootstrap_text("D:\\demo")
    assert "必做" in t, "R879 的写入分支只有代码知道，agent 不会带这枚标记"
    assert "【必须照做】" in t, "没说清写了换来什么 = 又一条纯义务"
    i = t.index("必做")
    seg = t[max(0, i - 200): i + 500]
    assert "【必须照做】" in seg, "标记与回报被拆到不相邻的位置，没人会跨段拼因果"
    assert "hub_memory_write" in seg, "没给写入口指引"


def test_bootstrap_warns_against_over_tagging():
    """104 条 pinned 老坑全标必做 = 噪声泛滥，引导块必须写明边界。"""
    t = core.bootstrap_text("D:\\demo")
    assert ("别标" in t or "不要标" in t or "不许标" in t), t


def test_mcp_tool_description_mentions_directive_tag():
    """工具描述是 agent 的 UI（desc_quality 口径），引导块之外这一面也要同步。"""
    tool = next(x for x in m.TOOLS if x["name"] == "hub_memory_write")
    assert "必做" in tool["description"], tool["description"]


def test_stale_v7_block_replaced_and_user_text_survives(tmp_path):
    p = tmp_path / "AGENTS.md"
    user_text = "# 我的规则\n别动这一段。\n"
    p.write_text(user_text + "\n" + OLD_BLOCK, encoding="utf-8")

    assert core.inject_bootstrap(str(p), "D:\\demo") == ""
    text = p.read_text(encoding="utf-8")
    assert "别动这一段。" in text, "注入把主人自己的内容冲掉了"
    assert core.bootstrap_version(str(p)) == "8"
    assert "必做" in text
    assert text.count(core.BOOTSTRAP_BEGIN) == 1, "旧块没被摘掉，注入了两块"
    assert "<!-- agenthub:version:7 -->" not in text
