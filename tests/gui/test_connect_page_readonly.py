# -*- coding: utf-8 -*-
"""gui 层：接入中心状态刷新必须只读（R33）。

守的是一类「看一样东西却改了它」的静默副作用：ConnectPage 曾把状态刷新
直接接到 core.deploy_server()——构造接入页（GUI 结构快照会实例化全部页面）
就往 ~/.agenthub/mcp_server 重写 11 个文件、留 .old-* 备份再删。在严格
沙箱里那 60+ 文件删除直接被拦，整层回归血红（2026-10-22 实测）。

双保险断言：
  1) 进程内 tripwire：boot 窗口并停留超过自动刷新定时器（400ms），
     core.deploy_server 一次都不许被调用；
  2) 磁盘无残留：~/.agenthub 下不产生 .mcp_server.old-* / .stage-* 目录。
"""
from pathlib import Path

from helpers import boot_window, settle


def test_connect_page_refresh_is_read_only(qapp, hub_root, monkeypatch):
    import os

    import core
    calls = []
    _real = core.deploy_server

    def tripwire(src_dir="", dst_dir=""):
        calls.append((src_dir, dst_dir))
        return _real(src_dir, dst_dir)

    monkeypatch.setattr(core, "deploy_server", tripwire)
    watch = Path(os.path.expanduser("~/.agenthub"))
    strays_before = sorted(p.name for p in watch.glob(".mcp_server.old-*")) + \
        sorted(p.name for p in watch.glob(".mcp_server.stage-*"))

    w = boot_window(qapp, hub_root)
    settle(qapp, max_s=3.0)   # 等过 QTimer.singleShot(400, refresh_status)

    assert not calls, (
        f"接入中心状态刷新调用了 deploy_server（只读视图不得触发部署）：{calls[:3]}")
    strays_after = sorted(p.name for p in watch.glob(".mcp_server.old-*")) + \
        sorted(p.name for p in watch.glob(".mcp_server.stage-*"))
    assert strays_after == strays_before, (
        f"刷新状态在 ~/.agenthub 留下部署残留：新增 {set(strays_after) - set(strays_before)}")
    assert w is not None
