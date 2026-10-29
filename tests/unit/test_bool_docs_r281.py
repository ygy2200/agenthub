# -*- coding: utf-8 -*-
r"""布尔参数的文档示例必须与类型契约一致（R281）。

R275 起布尔参数只接受 true/false（"false"/1/0 都会被拒），但工具描述里仍教
`unread_only=1`——模型照文档传参就会被拒。文档与 schema 打架比没文档更糟：
这里按 schema 扫描所有工具描述与参数说明，禁止布尔参数用 1/0 或带引号的值做示例。
"""
import re

import brain
import agenthub_mcp as m

# 1/0 或带引号的 true/false 都是坏例子；裸 true/false 是合法 JSON 布尔，放行
_BAD_EXAMPLE = re.compile(
    r"(?P<name>[a-z_]+)\s*=\s*(?:"
    r"[\"'](?:1|0|true|false)[\"']"      # 带引号
    r"|1|0"                              # 数字
    r")\b", re.I)


def _bool_props(t):
    props = (t.get("inputSchema") or {}).get("properties") or {}
    return {k for k, v in props.items() if (v or {}).get("type") == "boolean"}


def _texts(t):
    yield t.get("description") or ""
    for p in (t.get("inputSchema") or {}).get("properties", {}).values():
        yield (p or {}).get("description") or ""


def test_no_boolean_param_documented_as_number_or_quoted():
    bad = []
    for t in m.TOOLS:
        bools = _bool_props(t)
        for text in _texts(t):
            for mo in _BAD_EXAMPLE.finditer(text):
                if mo.group("name").lower() in bools:
                    bad.append((t["name"], mo.group(0)))
    assert not bad, "布尔参数被文档教成非布尔值：" + repr(bad[:10])


def test_signals_description_teaches_true():
    t = next(x for x in m.TOOLS if x["name"] == "hub_signals")
    assert "unread_only=true" in t["description"], t["description"]


def test_runtime_matches_docs(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_signals", {"agent": "qa", "unread_only": 1}, hub_root)
    assert "必须是布尔值" in out, out
    out = m.call_tool("hub_signals", {"agent": "qa", "unread_only": True}, hub_root)
    assert "必须是布尔值" not in out, out
