# -*- coding: utf-8 -*-
"""错误文案审计器（R180）：相邻字符串拼接不截断，纯透传包装不误报。"""
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"


@pytest.fixture
def audit(monkeypatch):
    monkeypatch.syspath_prepend(str(TOOLS))
    import audit_errmsg
    return audit_errmsg


def test_extracts_concatenated_literals(audit):
    src = ('def f():\n'
           '    return (f"错误：权限不足，" \n'
           '            "请用 --agent <名> 重新接入。")\n')
    msgs = audit.extract_errors(src)
    assert len(msgs) == 1
    assert msgs[0].startswith("错误：权限不足")
    assert "请用 --agent" in msgs[0], "第二段动作提示被截断"


def test_wrapper_is_not_candidate(audit):
    msgs = audit.extract_errors('def f(err):\n    return f"错误：{err}"\n')
    candidates, wrappers = audit.classify(msgs)
    assert not candidates and wrappers


def test_allowed_values_and_examples_are_actionable(audit):
    msgs = audit.extract_errors(
        'def f():\n    return "错误：action 只能是 a/b"\n'
        'def g():\n    return "错误：query 必填（如「上线检查」）"\n')
    candidates, wrappers = audit.classify(msgs)
    assert not candidates and not wrappers


def test_bare_not_found_is_candidate(audit):
    msgs = audit.extract_errors(
        'def f(x):\n    return f"错误：记录 #{x} 不存在"\n')
    candidates, wrappers = audit.classify(msgs)
    assert candidates and not wrappers
