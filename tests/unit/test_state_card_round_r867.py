# -*- coding: utf-8 -*-
"""R867：续跑点轮次只认「这轮干了什么」，不把「下一步 R867」当进度。

实锤：R866 的工作记录正文是「目的：R866 …。下一步方向：R867 接 zcode…」，
旧 `sync_state_card` 扫全文取最大轮次，卡片从 866 跳到 867——下一轮开局
会虚报「已到 867」。这类「下一步引用」在每轮记录里几乎必有，必须先把
进度轮次和候选轮次分开。

口径：目的段 > 「下一步/下一轮/下一批」标记之前的正文 > 全文兜底
（兜底只为兼容没有「目的」的历史记录）。
"""
import json
from pathlib import Path

import brain


BASE = {"schema": 1, "target_rounds": 1500, "current_round": 865,
        "version": "v2.41.216", "updated": "2026-11-04"}


def _card(root, project, payload):
    out = Path(root) / project / "output"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "迭代状态.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_next_step_round_does_not_advance(hub_root):
    path = _card(hub_root, "某项目", dict(BASE, current_round=866))
    upd = brain.sync_state_card(
        hub_root, "某项目",
        "目的：R866 续跑点版本。做了什么：改 brain.py。下一步方向：R867 接 zcode 依从缺口。",
        agent="qoder")
    assert upd == {}, upd
    assert json.loads(path.read_text(encoding="utf-8"))["current_round"] == 866


def test_purposeless_record_stops_at_next_marker(hub_root):
    path = _card(hub_root, "某项目", dict(BASE))
    upd = brain.sync_state_card(
        hub_root, "某项目",
        "R866 收口。下一步方向：R867 接 zcode 依从缺口。", agent="qoder")
    assert upd.get("round") == 866, upd
    assert json.loads(path.read_text(encoding="utf-8"))["current_round"] == 866


def test_purpose_wins_over_next_step(hub_root):
    path = _card(hub_root, "某项目", dict(BASE))
    upd = brain.sync_state_card(
        hub_root, "某项目",
        "目的：R866 本轮收口。下一步 R868 再说。", agent="qoder")
    assert upd.get("round") == 866, upd
    assert json.loads(path.read_text(encoding="utf-8"))["current_round"] == 866


def test_plain_record_still_advances(hub_root):
    path = _card(hub_root, "某项目", dict(BASE))
    upd = brain.sync_state_card(hub_root, "某项目", "R866 收口，回顾 R850 的结论",
                                agent="qoder")
    assert upd.get("round") == 866, upd
    assert json.loads(path.read_text(encoding="utf-8"))["current_round"] == 866
