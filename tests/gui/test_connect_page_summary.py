# -*- coding: utf-8 -*-
"""gui 层：接入中心汇总行给出完成度与唯一优先的下一步（R175）。

卡片逐张展开时，用户要读七张卡才知道「还差什么、先做哪步」。汇总行必须：
  1) 数字与卡片一致（MCP/引导/服务端状态/手动配置数）；
  2) 下一步只给一个最高优先级动作（Python > 部署 > 一键接入 > 注入引导 > 手动片段 > 已完成）；
  3) 细节放 tooltip，不把七个 agent 名塞进主文案。
用假 adapter/状态驱动，不写任何真实配置。
"""
from PySide6.QtWidgets import QWidget

from helpers import settle, track_window


def _adapter(name, mcp=True, boot=True):
    return {"agent": name,
            "mcp": {"path": f"/fake/{name}.json", "layout": "standard"} if mcp else None,
            "instr_path": f"/fake/{name}.instr" if boot else ""}


def _state(state="ready", py="D:/python.exe"):
    return {"python": py, "server": "" if state == "missing" else "/fake/server",
            "state": state, "diff": [], "absent": [], "same": 11}


def _page(qapp, hub_root, monkeypatch, adapters, st, connected=(), booted=()):
    import core
    from pages.connect_page import ConnectPage

    monkeypatch.setattr(core, "load_agent_adapters", lambda root="": list(adapters))
    monkeypatch.setattr(core, "mcp_has_agenthub",
                        lambda path, layout="": path in set(connected))
    monkeypatch.setattr(core, "bootstrap_status", lambda path: path in set(booted))
    monkeypatch.setattr(core, "deploy_status", lambda *a, **k: dict(st))

    win = QWidget()
    win.root = str(hub_root)
    track_window(win)          # B019：不登记的裸窗口不销毁，qfw 注册表会把整页留住
    page = ConnectPage(win, win)
    settle(qapp, max_s=0.6)   # 等过 singleShot(400, refresh_status)
    page.refresh_status()
    return win, page


def test_connect_summary_all_ready(qapp, hub_root, monkeypatch):
    win, page = _page(
        qapp, hub_root, monkeypatch,
        [_adapter("a"), _adapter("b")],
        _state(),
        connected=("/fake/a.json", "/fake/b.json"),
        booted=("/fake/a.instr", "/fake/b.instr"))
    try:
        text = page.sumLabel.text()
        assert "MCP 2/2 已接入" in text and "引导 2/2 已注入" in text
        assert "服务端就绪" in text
        assert "下一步：无" in text
        assert page.sumLabel.toolTip() == ""
        assert "mcpServers" in page.snippet.toPlainText() and '"agenthub"' in page.snippet.toPlainText()
        assert page.introLabel.wordWrap(), "顶部说明必须换行，不能截断"
    finally:
        win.close()


def test_connect_summary_next_step_priority(qapp, hub_root, monkeypatch):
    import core
    adapters = [_adapter("a"), _adapter("b"), _adapter("c", mcp=False, boot=True)]
    booted = {"/fake/a.instr", "/fake/b.instr"}
    connected = {"/fake/a.json"}
    win, page = _page(qapp, hub_root, monkeypatch, adapters, _state("stale"),
                      connected=connected, booted=booted)
    try:
        text = page.sumLabel.text()
        assert "MCP 1/3 已接入" in text and "引导 2/3 已注入" in text
        assert "服务端陈旧" in text and "1 个需手动配置" in text
        assert "下一步：点右上「部署服务端」同步服务端文件" in text
        tip = page.sumLabel.toolTip()
        assert "未接入：b" in tip and "需手动接入：c" in tip

        monkeypatch.setattr(core, "deploy_status", lambda *a, **k: _state("ready"))
        page.refresh_status()
        assert "下一步：给 1 个未接入的 agent 点「一键接入」" in page.sumLabel.text()

        connected.add("/fake/b.json")
        monkeypatch.setattr(core, "mcp_has_agenthub",
                            lambda path, layout="": path in connected)
        page.refresh_status()
        assert "下一步：给 1 个 agent 点「注入引导」" in page.sumLabel.text()

        booted.add("/fake/c.instr")
        monkeypatch.setattr(core, "bootstrap_status", lambda path: path in booted)
        page.refresh_status()
        assert "下一步：1 个未探测到配置的 agent 用下方片段手动接入" in page.sumLabel.text()
    finally:
        win.close()


def test_connect_summary_python_missing_and_empty(qapp, hub_root, monkeypatch):
    win, page = _page(qapp, hub_root, monkeypatch, [_adapter("a")],
                      _state("no-python", py=""), connected=(), booted=())
    try:
        assert "服务端缺少 Python" in page.sumLabel.text()
        assert "下一步：先安装 Python" in page.sumLabel.text()
    finally:
        win.close()

    win2, page2 = _page(qapp, hub_root, monkeypatch, [], _state())
    try:
        assert "还没有登记任何 agent" in page2.sumLabel.text()
        assert page2.sumLabel.toolTip() == ""
    finally:
        win2.close()
