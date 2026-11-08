# -*- coding: utf-8 -*-
"""R1014（B042/B098）：error 档预算与采纳率倒挂的收口。

真库 30 天：
  · error：39 注入 / 38 可指纹 / 18 被用 = 47.4%
  · strong：198 注入 / 195 可指纹 / 45 被用 = 23.1%
  · 预算却反着：prompt=8 / error=6
  · 近 30 天 error 只掐断 5 次（其中 1 次明确 used=6/6）

所以把 error 从 6 对齐到 8，而不是取消预算——重试循环仍要被硬闸挡住。
"""
import json

import brain


def test_error_budget_aligned_to_prompt():
    assert brain.NUDGE_BUDGET_DEFAULTS == {"prompt": 8, "error": 8}


def test_error_budget_blocks_at_eight_not_six(tmp_path):
    budget = tmp_path / "nudge.json"
    budget.write_text(json.dumps({
        "v": 1, "sessions": {"workbuddy|s1": {"error": {"n": 7}}}
    }), encoding="utf-8")
    peek = brain.nudge_peek("root", "s1", "error", agent="workbuddy", path=str(budget))
    assert peek["used"] == 7 and peek["limit"] == 8 and peek["allowed"] is True
    brain.nudge_commit(peek)
    over = brain.nudge_peek("root", "s1", "error", agent="workbuddy", path=str(budget))
    assert over["used"] == 8 and over["allowed"] is False, over
