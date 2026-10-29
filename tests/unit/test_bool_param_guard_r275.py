# -*- coding: utf-8 -*-
r"""布尔入参守卫（R275）：schema 声明 boolean ⇒ 只接受真正的布尔值。

R274 把整数参数收进集中校验后，布尔参数仍是两个反向的静默错误：
① handler 普遍 `bool(arguments.get(...))`——字符串 "false" 是真值，预演/关闭类开关
   会被反转（hub_export(apply="false") 真清理）；
② 协议层用 `is True`/`is False` 判定 dry_run/confirm——传 "true" 不预演而是**真写库**，
   传 "false" 绕过人工确认闸门（fail-open）。
这里按 schema 运行时枚举，一律拒绝非布尔值；restore 保留其刻意的字符串兼容。
"""
import brain
import agenthub_mcp as m

EXEMPT = {"restore"}


def _schema_bool_props():
    for t in m.TOOLS:
        props = (t.get("inputSchema") or {}).get("properties", {}) or {}
        for pname, spec in props.items():
            if (spec or {}).get("type") == "boolean" and pname not in EXEMPT:
                yield t["name"], pname


def test_all_declared_booleans_reject_non_bool():
    seen = 0
    for tool, pname in _schema_bool_props():
        seen += 1
        for bad in ("false", "true", "0", 0, 1):
            out = m._bad_bool_param(tool, {pname: bad})
            assert out and "必须是布尔值" in out, f"{tool}.{pname} 传 {bad!r} 未拒绝"
    # 14 个业务布尔 + 写工具注入的 dry_run/confirm
    assert seen >= 16, seen


def test_real_booleans_and_absent_pass():
    for tool, pname in _schema_bool_props():
        assert m._bad_bool_param(tool, {pname: True}) is None
        assert m._bad_bool_param(tool, {pname: False}) is None
        assert m._bad_bool_param(tool, {pname: None}) is None
        assert m._bad_bool_param(tool, {}) is None


def test_restore_keeps_string_compat():
    assert m._bad_bool_param("hub_archive_project", {"restore": "true"}) is None


def test_dry_run_string_does_not_write(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_task_add", {"title": "不该落库", "dry_run": "true"}, hub_root)
    assert "必须是布尔值" in out, out[:200]
    assert brain.task_count(hub_root) == 0


def test_confirm_string_false_does_not_execute(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_task_add", {"title": "确认闸门", "confirm": "false"}, hub_root)
    assert "必须是布尔值" in out, out[:200]
    assert brain.task_count(hub_root) == 0


def test_apply_string_false_is_not_applied(hub_root):
    out = m.call_tool("hub_export", {"action": "prune", "apply": "false"}, hub_root)
    assert "必须是布尔值" in out, out[:200]


def test_unread_only_string_rejected(hub_root):
    out = m.call_tool("hub_signals", {"agent": "qa", "unread_only": "false"}, hub_root)
    assert "必须是布尔值" in out, out[:200]


def test_proxy_gets_same_rejection(hub_root):
    brain.init_db(hub_root)
    out = m.call_tool("hub_call_tool", {
        "name": "hub_task_add",
        "arguments": {"title": "代理坏布尔", "dry_run": "true"}}, hub_root)
    assert "必须是布尔值" in out, out[:200]
    assert brain.task_count(hub_root) == 0
