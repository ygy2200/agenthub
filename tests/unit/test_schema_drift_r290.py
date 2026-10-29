# -*- coding: utf-8 -*-
r"""schema_drift 检查器要在函数拆分后仍然有效（R290）。

R283 把 `call_tool` 拆成薄壳 + `_call_tool_impl` 分发函数；检查器仍按名字取
`call_tool`，拿到的是没有分发逻辑的壳 → 62 处「声明了没实现」全是假警报。
修复：按「含 name == hub_* 比较最多」选函数；一个分发块都找不到就明确报失效。
"""
import re

import ops


def test_schema_drift_is_green_after_r283_split():
    r = ops._schema_drift()
    assert r["ok"] is True, r
    assert not r["detail"], r
    m = re.search(r"(\d+) 个带 action enum", r["summary"])
    assert m and int(m.group(1)) >= 10, r["summary"]
