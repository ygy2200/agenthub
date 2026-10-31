# -*- coding: utf-8 -*-
"""帮助页跟上工作台新能力（R810）。

帮助页落后于界面比没有帮助更糟：用户会照着说明找按钮。R807 筛选、R809 补阻塞原因、
R808 详情字段都要在「工作台」一行里出现；CLI 速查也不该再写「等待依赖」这种
把外部阻塞算进去的旧口径。
"""
from pages.help_page import HelpPage


def test_workbench_line_covers_new_desktop_actions():
    c = HelpPage.CONTENT
    for phrase in ("即时筛选", "补改阻塞原因", "技能要求、交付物与完成审批"):
        assert phrase in c, f"帮助页漏了新能力：{phrase}"


def test_cli_next_uses_the_same_wait_vocabulary():
    c = HelpPage.CONTENT
    assert "等待中（等前置任务或外部阻塞" in c, "CLI 速查仍用旧口径"
    assert "等待依赖</code>" not in c, "CLI 速查还写着「等待依赖」"
