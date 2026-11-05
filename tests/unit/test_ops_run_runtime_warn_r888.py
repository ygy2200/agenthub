# -*- coding: utf-8 -*-
"""R888 / B093：`hub_ops_run` 是 R870「旧进程自述」漏掉的第四个出口。

R887 实测撞到：deploy 16/16 之后，MCP 侧 `hub_ops_run(stale_health)` 仍返回旧摘要
（长驻进程 12:43 启动，内存里 import 的还是旧 ops.py），新的「取代声明未裁决」点名
对走 MCP 的调用方完全不可见。R870 已在 session_brief / hub_log_work / hub_health
三处加了自述，`hub_ops_run` 没有 —— 检查结果会被当成现状，比「没部署」更难排查。

本轮只补「自述 + 说清结论来自旧代码」，复用现成的 `_runtime_warn()`，
不擅自改成自动热重载（部署≠生效、重启各 agent 是既定运维口径，属方向级变更）。
"""
import agenthub_mcp as mcp
import ops


def _fake(monkeypatch, result):
    monkeypatch.setattr(ops, "run_check", lambda *a, **k: result)


def test_stale_process_result_is_labelled(monkeypatch, hub_root):
    """告警要出现、要排在明细前、要点破「结论由旧代码算出」。"""
    _fake(monkeypatch, {"ok": True, "summary": "git 工作区干净", "detail": ["明细1"]})
    monkeypatch.setattr(mcp, "_runtime_warn",
                        lambda: "⚠ 当前 MCP 进程运行的是旧代码（1 个文件：ops.py）")
    out = mcp.call_tool("hub_ops_run", {"name": "git_status", "target": str(hub_root)}, hub_root)
    assert "git 工作区干净" in out
    assert "旧代码" in out
    assert "不代表当前源码" in out, "必须说清结论的适用范围"
    assert out.index("旧代码") < out.index("明细1"), "告警排在明细前，别被 detail[:8] 挤掉"


def test_fresh_process_has_no_warning(monkeypatch, hub_root):
    _fake(monkeypatch, {"ok": True, "summary": "git 工作区干净", "detail": []})
    monkeypatch.setattr(mcp, "_runtime_warn", lambda: "")
    out = mcp.call_tool("hub_ops_run", {"name": "git_status", "target": str(hub_root)}, hub_root)
    assert "旧代码" not in out
    assert "不代表当前源码" not in out


def test_verdict_symbol_is_not_changed_by_warning(monkeypatch, hub_root):
    """告警不许把 ✗ 洗成 ✓ —— 失败的检查仍然要显红。"""
    _fake(monkeypatch, {"ok": False, "summary": "检查未过", "detail": []})
    monkeypatch.setattr(mcp, "_runtime_warn", lambda: "⚠ 旧代码")
    out = mcp.call_tool("hub_ops_run", {"name": "regression", "target": str(hub_root)}, hub_root)
    assert out.startswith("✗"), out
    assert "检查未过" in out


def test_warn_probe_failure_does_not_break_check(monkeypatch, hub_root):
    """版本探针自己炸了，检查本身照常出结果（fail-open，但不能静默丢结果）。"""
    _fake(monkeypatch, {"ok": True, "summary": "git 工作区干净", "detail": []})

    def boom():
        raise RuntimeError("模拟版本探针异常")

    monkeypatch.setattr(mcp, "_runtime_warn", boom)
    out = mcp.call_tool("hub_ops_run", {"name": "git_status", "target": str(hub_root)}, hub_root)
    assert "git 工作区干净" in out
    assert "版本探针" in out or "不可用" in out, "探针失败也要留痕"
