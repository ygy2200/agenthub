# -*- coding: utf-8 -*-
r"""整数入参守卫（R274）：schema 声明 integer ⇒ 必须走集中校验。

Round13 只盯源码里的 `int(arguments.get("X"`，漏了两种形态：
① handler 根本不读这个参数（hub_signals 的 limit 声明了却没生效）；
② 参数原样交给下游，下游 `int()` 失败时静默回落默认（priority/keep_bak…）。
这里改成 schema 驱动：所有 integer 参数必须登记进 `_INT_PARAMS`（max_chars 属
结果预算，明确豁免），且登记表不能残留 schema 里不存在的参数。
"""
import ast
from pathlib import Path

import agenthub_mcp as m

SRC = Path(m.__file__).with_suffix(".py")
GLOBAL_PARAMS = {"max_chars", "dry_run", "confirm", "idem_key"}


def _schema_int_props():
    for t in m.TOOLS:
        props = (t.get("inputSchema") or {}).get("properties", {}) or {}
        for pname, spec in props.items():
            if (spec or {}).get("type") == "integer" and pname not in GLOBAL_PARAMS:
                yield t["name"], pname


def _handler_segments():
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    blocks = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                and t.left.id == "name" and len(t.ops) == 1
                and isinstance(t.ops[0], ast.Eq)
                and isinstance(t.comparators[0], ast.Constant)
                and isinstance(t.comparators[0].value, str)
                and t.comparators[0].value.startswith("hub_")):
            blocks.setdefault(t.comparators[0].value, []).append(node)
    return {k: "\n".join("\n".join(lines[n.lineno - 1:n.end_lineno]) for n in v)
            for k, v in blocks.items()}


def test_every_schema_int_param_is_centrally_validated():
    gaps = [(n, p) for n, p in _schema_int_props()
            if p not in m._INT_PARAMS.get(n, ()) and p not in m._INT_EXEMPT_PARAMS]
    assert not gaps, f"schema 声明整数却未集中校验：{gaps}"


def test_registry_has_no_stale_entries():
    props = {t["name"]: set((t.get("inputSchema") or {}).get("properties", {}) or {})
             for t in m.TOOLS}
    stale = [(n, p) for n, ps in m._INT_PARAMS.items() for p in ps
             if p not in props.get(n, set())]
    assert not stale, f"校验表残留 schema 未声明的参数：{stale}"


def test_every_business_param_reaches_handler():
    """声明了却完全不读 = 静默吞参数（hub_signals limit 曾如此）。"""
    blocks = _handler_segments()
    problems = []
    for t in m.TOOLS:
        name = t["name"]
        seg = blocks.get(name, "")
        assert seg, f"{name} 找不到 handler 分支"
        for pname in ((t.get("inputSchema") or {}).get("properties", {}) or {}):
            if pname in GLOBAL_PARAMS:
                continue
            if pname not in seg:
                problems.append((name, pname))
    assert not problems, f"schema 声明但 handler 从未读取：{problems}"


def test_bad_int_gets_actionable_error(hub_root):
    cases = [
        ("hub_task_add", {"title": "x", "priority": "abc"}),
        ("hub_claim", {"task_id": "abc", "agent": "qa"}),
        ("hub_export", {"action": "prune", "keep_bak": "很多"}),
        ("hub_freeze", {"action": "freeze", "minutes": "半小时", "reason": "测试"}),
    ]
    for tool, args in cases:
        out = m.call_tool(tool, args, hub_root)
        assert "必须是整数" in out, f"{tool} 坏整数未拒绝：{out[:200]}"


def test_good_numeric_string_still_works(hub_root):
    import brain
    brain.init_db(hub_root)
    err, tid = brain.task_add(hub_root, "任务")
    assert not err
    out = m.call_tool("hub_claim", {"task_id": str(tid), "agent": "qa"}, hub_root)
    assert "认领成功" in out, out[:200]
