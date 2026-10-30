# -*- coding: utf-8 -*-
"""帮助页与桌面能力同步（R796）。

帮助页过去写「工作台可直接认领」——桌面端从来没有认领入口（不代理 agent 身份），
这类过期说明比没有说明更糟：用户会去找一个不存在的按钮。这里钉住工作台说明
必须列出桌面真实动作，并明确认领仍归 agent。
"""
from pages.help_page import HelpPage


def test_workbench_help_matches_desktop_actions(qapp):
    content = HelpPage.CONTENT
    assert "可直接认领" not in content
    for action in ("新建任务", "标记完成", "记录失败重试", "取消", "调优先级",
                   "补详情/截止日/前置依赖"):
        assert action in content, f"帮助页漏了桌面动作：{action}"
    assert "认领仍由 agent 做" in content
