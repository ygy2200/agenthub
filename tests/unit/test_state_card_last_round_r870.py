# -*- coding: utf-8 -*-
"""R870：续跑点的 last_round 不能与卡片本体互相矛盾。

实锤：生产卡片 current_round=869/version=v2.41.219，last_round 却是
round=861/version=v2.41.212（R861 时代手写字段），theme 已经是最新。简报读
last_round 就会把 861 当「上轮」。轮次/版本只镜像卡片当前值——绝不因为
补一条旧记录而回退进度。
"""
import json
from pathlib import Path

import brain


def _card(root, project, payload):
    out = Path(root) / project / "output"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "迭代状态.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


BASE = {
    "schema": 1, "target_rounds": 1500, "current_round": 868, "version": "v2.41.219",
    "updated": "2026-11-04",
    "last_round": {"round": 861, "version": "v2.41.212", "theme": "旧主题",
                   "record_id": 1, "date": "2026-11-01"},
}


def test_advance_heals_last_round(hub_root):
    path = _card(hub_root, "某项目", dict(BASE))
    upd = brain.sync_state_card(hub_root, "某项目", "目的：R870 旧进程自述与卡片自洽。",
                                agent="qoder", record_id=9, date="2026-11-04",
                                version="v2.41.220")
    assert upd["round"] == 870, upd
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["current_round"] == 870
    assert data["version"] == "v2.41.220"
    assert data["last_round"]["round"] == 870, "last_round 还停在旧轮次"
    assert data["last_round"]["version"] == "v2.41.220", "last_round 还停在旧版本"
    assert data["last_round"]["record_id"] == 9


def test_old_record_does_not_regress_last_round(hub_root):
    """补一条历史记录（R862）不能把 last_round 拉回 862。"""
    payload = dict(BASE, current_round=900, version="v2.41.230")
    payload["last_round"] = {"round": 861, "version": "v2.41.212", "theme": "旧"}
    path = _card(hub_root, "某项目", payload)
    upd = brain.sync_state_card(hub_root, "某项目", "回顾 R862 的结论", agent="qoder")
    assert upd.get("round") is None, upd
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["current_round"] == 900
    assert data["last_round"]["round"] == 900, "last_round 被旧记录拉回去了"
    assert data["last_round"]["version"] == "v2.41.230"


def test_missing_last_round_version_is_filled(hub_root):
    payload = dict(BASE, last_round={"round": 868, "theme": "旧主题"})
    path = _card(hub_root, "某项目", payload)
    brain.sync_state_card(hub_root, "某项目", "目的：R870 收口。", agent="qoder",
                          version="v2.41.220")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["last_round"]["round"] == 870
    assert data["last_round"]["version"] == "v2.41.220"
    assert data["last_round"]["theme"] == "R870 收口"
