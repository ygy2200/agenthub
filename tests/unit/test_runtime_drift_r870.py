# -*- coding: utf-8 -*-
"""R870：长驻 MCP 进程跑旧代码，必须自己说出来。

为什么改（R869 实测）：部署副本已是 2.41.219，但 qoder 的 MCP 进程是 16:55 启动的
2.41.212。`hub_log_work` 明明返回「已记入大脑」，续跑点卡片却停在 868——因为旧进程里
根本没有 R862 的同步代码。回复里没有任何征兆，模型会把旧工具的行为当成现状。

本轮把「进程版本落后」接到三条必经通道：
  1. 会话简报第一屏（SessionStart 钩子 / MCP 握手都走 session_brief）；
  2. hub_log_work 的结果（写记录时当场说明「卡片同步可能没跑到」）；
  3. hub_health（体检结论只代表旧代码，别拿旧视角当现状）。

判定只认运行标记 + 内容指纹，不猜、不探活；无标记时静默（测试/CLI 进程不该误报）。
"""
import json
import time
from pathlib import Path

import agenthub_mcp
import brain
import core


def _marker(marker_dir, root, agent, base, started, fingerprint, version="2.41.212"):
    marker_dir.mkdir(parents=True, exist_ok=True)
    p = marker_dir / f"serve-{agent}-{time.time_ns()}.json"
    p.write_text(json.dumps({
        "pid": 12345, "agent": agent, "root": str(root), "base": str(base),
        "started": started, "fingerprint": fingerprint, "version": version,
        "ts": "2026-11-04T18:00:00",
    }, ensure_ascii=False), encoding="utf-8")
    return p


def _fake_server(base: Path, version="2.41.212"):
    base.mkdir(parents=True, exist_ok=True)
    (base / "agenthub_mcp.py").write_text(f'SERVER_INFO = {{"name": "agenthub", "version": "{version}"}}\n',
                                          encoding="utf-8")
    (base / "brain.py").write_text("# old brain\n", encoding="utf-8")
    return core.file_fingerprints(str(base))


def test_agent_staleness_filters_by_agent_and_root(hub_root, tmp_path, monkeypatch):
    """别的 agent / 别的数据根的旧进程，不能算到当前 agent 头上。"""
    monkeypatch.setattr(core, "RUNTIME_MARKER_DIR", tmp_path / "runtime")
    base = tmp_path / "mcp_server"
    fp = _fake_server(base)
    _marker(core.RUNTIME_MARKER_DIR, hub_root, "qoder", base, "2026-11-04T16:55:19", fp)
    (base / "agenthub_mcp.py").write_text("# changed after startup\n", encoding="utf-8")

    got = core.agent_staleness(hub_root, "qoder")
    assert got["known"] and got["count"] == 1, got
    assert got["version"] == "2.41.212", got
    assert core.agent_staleness(hub_root, "workbuddy")["known"] is False
    assert core.agent_staleness(str(tmp_path / "另一个根"), "qoder")["known"] is False


def test_brief_warns_only_its_own_agent(hub_root, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "RUNTIME_MARKER_DIR", tmp_path / "runtime")
    base = tmp_path / "mcp_server"
    fp = _fake_server(base)
    _marker(core.RUNTIME_MARKER_DIR, hub_root, "qoder", base, "2026-11-04T16:55:19", fp)
    (base / "brain.py").write_text("# changed after startup\n", encoding="utf-8")
    brain.init_db(hub_root)

    mine = brain.session_brief(hub_root, agent="qoder")["text"]
    assert "跑旧代码" in mine and "重启客户端" in mine, mine[:400]
    other = brain.session_brief(hub_root, agent="workbuddy")["text"]
    assert "跑旧代码" not in other, other[:400]


def test_no_marker_means_no_warning(hub_root):
    """无运行标记（测试/一次性 CLI）不猜——宁可不报，也不能误报。"""
    brain.init_db(hub_root)
    text = brain.session_brief(hub_root, agent="qoder")["text"]
    assert "跑旧代码" not in text, text[:400]
    assert core.agent_staleness(hub_root, "qoder")["known"] is False


def test_log_work_says_card_may_lag(hub_root, monkeypatch):
    """旧进程写记录时，回复必须自己点破「续跑点同步可能没跑到」。"""
    brain.init_db(hub_root)
    brain.add_record(hub_root, "漂移项目", "workbuddy", "2026-11-04", "占位", "占位")
    fake = {"known": True, "count": 2,
            "stale": [{"file": "brain.py", "mtime": "2026-11-04T17:47:36"},
                      {"file": "agenthub_mcp.py", "mtime": "2026-11-04T17:48:40"}],
            "started": "2026-11-04T16:55:19", "basis": "content"}
    monkeypatch.setattr(core, "runtime_staleness", lambda *a, **k: fake)
    out = agenthub_mcp.call_tool("hub_log_work", {
        "project": "漂移项目", "agent": "workbuddy",
        "content": ("目的：R870 旧进程自述。做了什么：加告警。"
                    "验证结果：单测。如何回滚：git revert。")}, hub_root)
    assert "records#" in out, out[:300]
    assert "跑旧代码" in out and "2 个文件" in out and "重启客户端" in out, out[:500]


def test_health_carries_drift_warning(hub_root, monkeypatch):
    monkeypatch.setattr(core, "runtime_staleness", lambda *a, **k: {
        "known": True, "count": 1,
        "stale": [{"file": "brain.py", "mtime": "2026-11-04T17:47:36"}],
        "started": "2026-11-04T16:55:19", "basis": "content"})
    brain.init_db(hub_root)
    out = agenthub_mcp.call_tool("hub_health", {}, hub_root)
    assert "跑旧代码" in out and "重启客户端" in out, out[:500]
