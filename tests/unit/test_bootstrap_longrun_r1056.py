# -*- coding: utf-8 -*-
"""R1056：用户「没说停就不许停」必须写进引导块，且所有 agent 收得到。

为什么是硬约束：2026-11-09 用户连说三次「不要停」，我三次以「本批收口/该汇报」结束回合。
规则若不进 BOOTSTRAP_TEXT，换会话/换 agent 就丢——这是「下次的我不知道这次的我被骂过」。
"""
import core


def test_bootstrap_contains_longrun_rule():
    t = core.bootstrap_text("/x")
    assert "长跑授权" in t
    assert "汇报 ≠ 停止" in t
    assert "持续执行授权" in t


def test_bootstrap_version_bumped():
    assert core.BOOTSTRAP_VERSION == "9", "改引导内容必须升版本，否则 refresh 不重写"


def test_rules_file_has_section():
    t = core.load_rules("D:/agent资料/zcode项目记录") if __import__("os").path.isdir(
        "D:/agent资料/zcode项目记录") else ""
    if t:
        assert "长跑授权" in t
