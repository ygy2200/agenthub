# -*- coding:utf-8 -*-
"""R858 · B058：引导块教了「出错要登记」，没教「登记了换来什么」。

B060/B061 把回环修通之后（登记侧/召回侧同一个键、`errors.sig` 生产上有数据），
`hub_report_error` 这条指引第一次有了兑现对象——但引导块里它仍然只是一句
「登记，方便任何人查询和撤销」。对 agent 来说这是**义务**，不是**收益**；
而这套机制的全部价值恰恰在「解决了不登记 = 下次自己重新撞一遍」。
R857 之后欠账账本里那 3 条 open 就是这么来的：撞过、修法写在对话里、散会即失忆。

第二条更隐蔽：B040 上线了 `tags=机制说明` 这枚标记（讲这套机制自己的正文里
全是报错词，不带标记就会被当成「上次的修法」召回），但**只有代码知道**。
引导块不提，agent 写记忆时不会带，标记就得靠事后清洗（R853 那次洗公共账本
181 行里 116 行是自家评测写的，就是这个成本）。

每条都先在 v6 引导块上红。
"""
import core

OLD_BLOCK = (
    f"{core.BOOTSTRAP_BEGIN}\n"
    "<!-- agenthub:version:6 -->\n"
    "## AgentHub 公用大脑（自动接入，请勿删除本标记块）\n"
    "- 出错/受阻/踩坑：hub_report_error(title, detail, undo) 登记，undo 写回滚方式\n"
    f"{core.BOOTSTRAP_END}\n"
)


def test_bootstrap_version_bumped_to_v7():
    """加了内容必须 +1 版本，否则 `boot_stale` 检测与 refresh 都看不见这次改动。"""
    assert core.BOOTSTRAP_VERSION == "7"


def test_bootstrap_states_the_payoff_of_registering():
    """登记不是义务宣传：要说清「撞错时大脑自动递修法」这条回报从哪来。"""
    t = core.bootstrap_text("D:\\demo")
    assert "同签名" in t, "引导块没提同签名命中——agent 不知道登记换来什么"
    assert "hub_report_error" in t
    # 收益与动作必须挨着说：分两段写等于没写（没人会跨段拼因果）
    i = t.index("hub_report_error")
    assert "同签名" in t[max(0, i - 400):i + 400], "登记与收益被拆到不相邻的位置"


def test_bootstrap_marks_mechanism_docs():
    """机制说明这枚标记得由写入口自己声明——引导块不提，没人会带。"""
    t = core.bootstrap_text("D:\\demo")
    assert "机制说明" in t, "B040 的标记只有代码知道，agent 写记忆时不会带"


def test_stale_v6_block_gets_replaced_and_user_text_survives(tmp_path):
    """旧块要被整体换掉（不是追加），主人的既有内容一个字不许丢。"""
    p = tmp_path / "AGENTS.md"
    user_text = "# 我的规则\n别动这一段。\n"
    p.write_text(user_text + "\n" + OLD_BLOCK, encoding="utf-8")

    assert core.inject_bootstrap(str(p), "D:\\demo") == ""
    text = p.read_text(encoding="utf-8")
    assert "别动这一段。" in text, "注入把主人自己的内容冲掉了"
    assert core.bootstrap_version(str(p)) == core.BOOTSTRAP_VERSION
    assert "同签名" in text and "机制说明" in text
    assert text.count(core.BOOTSTRAP_BEGIN) == 1, "旧块没被摘掉，注入了两块"
    assert "<!-- agenthub:version:6 -->" not in text
