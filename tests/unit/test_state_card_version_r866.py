# -*- coding: utf-8 -*-
"""R866：续跑点版本不靠正文自觉——hub_log_work 落记录时把服务端当前版本带过去。

病根（R864/R865 实锤）：`sync_state_card` 只从正文里抽 `vX.Y.Z`。可 agent 按铁律写
的工作记录常常只写「R865 …」，不写版本——于是卡片版本停在 v2.41.215，而代码已经
2.41.216。简报读卡片，下一轮就会拿着旧版本号说话，还是「要人提醒才更新」。

边界：
  1. 显式 version 参数与正文版本取较大者，只进不退（卡片不会因为一次旧记录回退）；
  2. version 允许 `2.41.216` / `v2.41.216` 两种写法；
  3. 真入口 `hub_log_work` 确实把 SERVER_INFO 带进 sync（不是只改函数签名）。
"""
import json
from pathlib import Path

import agenthub_mcp
import brain


BASE = {"schema": 1, "target_rounds": 1500, "current_round": 865,
        "version": "v2.41.215", "updated": "2026-11-04"}


def _card(root, project, payload):
    out = Path(root) / project / "output"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "迭代状态.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_version_param_updates_without_body_version(hub_root):
    path = _card(hub_root, "某项目", dict(BASE))
    upd = brain.sync_state_card(hub_root, "某项目", "目的：R866 收口。", agent="qoder",
                                version="2.41.216")
    assert upd.get("version") == "v2.41.216", upd
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == "v2.41.216"
    assert data["current_round"] == 866


def test_version_param_never_downgrades(hub_root):
    path = _card(hub_root, "某项目", dict(BASE, version="v2.41.220"))
    upd = brain.sync_state_card(hub_root, "某项目", "回顾 v2.41.216 的结论",
                                agent="qoder", version="v2.41.216")
    assert "version" not in upd, upd
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == "v2.41.220"


def test_version_takes_max_of_body_and_param(hub_root):
    path = _card(hub_root, "某项目", dict(BASE))
    upd = brain.sync_state_card(hub_root, "某项目", "本轮 v2.41.219 先到",
                                agent="qoder", version="2.41.216")
    assert upd.get("version") == "v2.41.219", upd
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == "v2.41.219"


def test_log_work_carries_server_version(hub_root):
    """真入口必须带 SERVER_INFO，否则改签名只是摆设。"""
    path = _card(hub_root, "迭代项目", dict(BASE))
    brain.init_db(hub_root)
    brain.add_record(hub_root, "迭代项目", "workbuddy", "2026-11-04", "占位", "占位")
    out = agenthub_mcp.call_tool("hub_log_work", {
        "project": "迭代项目", "agent": "workbuddy",
        "content": ("目的：R866 版本不靠正文。做了什么：改 sync_state_card。"
                    "验证结果：单测。如何回滚：git revert。")},
        hub_root)
    assert "续跑点已自动更新" in out, out[:300]
    want = "v" + agenthub_mcp.SERVER_INFO["version"]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == want, (data["version"], want)
    assert want in out, out[:300]
