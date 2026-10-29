# -*- coding: utf-8 -*-
r"""R299·引导文案里的工具调用示例必须是真参数名。

dogfood 发现：`hub_health` 空脑引导与注入各 agent 的引导块都写
`hub_create_project(name="对象-问题")`，而该工具必填参数是 `project`——
照抄指引第一步就报「必填参数 project 缺失」，随后 agent 会误以为大脑没建好。
同类漂移（改名/加别名时忘了改文案）不该靠人眼发现，这里把文案里的
`hub_xxx(param=` 逐个对照 TOOLS 的真实 schema。
"""
import re

import agenthub_mcp as m
import brain
import core
import pytest

CALL_RE = re.compile(r"\b(hub_[a-z_]+)\(([^)\n]*)\)")


def _schema_props(tool: str) -> set:
    for spec in m.TOOLS:
        if spec.get("name") == tool:
            return set(((spec.get("inputSchema") or {}).get("properties") or {}))
    return set()


def _kwarg_hints(text: str):
    """抽 (工具名, 参数名) 二元组——只取 `name=...` 形式的显式 kwargs。"""
    out = []
    for tool, inner in CALL_RE.findall(text or ""):
        for raw in inner.split(","):
            k, sep, _v = raw.partition("=")
            k = k.strip()
            if sep and re.fullmatch(r"[a-z_]+", k):
                out.append((tool, k))
    return out


def test_hints_in_bootstrap_text_match_schema():
    text = core.bootstrap_text("D:/data")
    hints = _kwarg_hints(text)
    assert hints, "引导块里应至少有一个带参数名的示例"
    bad = [(t, p) for t, p in hints
           if t in {s["name"] for s in m.TOOLS} and p not in _schema_props(t)]
    assert not bad, f"引导块示例用了不存在的参数：{bad}"


def test_hints_in_health_empty_brain_match_schema(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_health", {}, hub_root)
    assert "空脑引导" in out
    hints = _kwarg_hints(out)
    bad = [(t, p) for t, p in hints
           if t in {s["name"] for s in m.TOOLS} and p not in _schema_props(t)]
    assert not bad, f"空脑引导用了不存在的参数：{bad}"


def test_create_project_hint_is_project_not_name(hub_root):
    """直接钉死本轮修的那条：两处文案都得是 project=。"""
    brain.init_db(hub_root)
    health = m.call_tool("hub_health", {}, hub_root)
    boot = core.bootstrap_text(hub_root)
    for text in (health, boot):
        assert 'hub_create_project(project=' in text
        assert 'hub_create_project(name=' not in text


def test_create_project_accepts_the_hinted_param(hub_root):
    """示例照抄能跑通——参数名对了才算修好。"""
    brain.init_db(hub_root)
    out = m.call_tool("hub_create_project", {"project": "对象-问题"}, hub_root)
    assert not m._is_error_text(out), out
    assert m._is_error_text(m.call_tool("hub_create_project", {"name": "对象-问题"}, hub_root))


def test_bootstrap_version_bumped_for_stale_detection():
    """内容变了版本必须变，否则旧块（含错提示）不会被识别为过时。"""
    assert core.BOOTSTRAP_VERSION == "6"
    assert f"agenthub:version:{core.BOOTSTRAP_VERSION}" in core.bootstrap_text("/x")
