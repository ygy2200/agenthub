# -*- coding: utf-8 -*-
"""R862：续跑点卡片自己跟上——干完活那条记录就是下一轮的起点。

为什么改：R838 让简报会读 `output/迭代状态.json`，但卡片仍靠人手动维护。忘记更新，
下一次会话的简报就报旧轮次，模型只能重新盘点——这仍是「要主人提醒才更新记忆」。
现在 `hub_log_work` 写完记录顺手把轮次/版本/主题/下一步回写进**已存在**的卡片。

保守边界（每条都有用例）：
  1. 只更新已存在的卡片，不给普通项目凭空造文件；
  2. 轮次只进不退；单次跳幅 ≤20；≥ target_rounds 的数字只当远期目标（「目标 R1500」）；
  3. 坏 JSON / 无权限静默跳过，记录写入不受影响；
  4. 未知字段原样保留，写前留一份 .bak-auto。

「修之前会怎样失败」：第 1 条在旧代码上必红——旧 `hub_log_work` 只写 records，
卡片还停在 861。
"""
import json
from pathlib import Path

import agenthub_mcp
import brain


def _card(root, project, payload):
    out = Path(root) / project / "output"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "迭代状态.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


BASE = {"schema": 1, "target_rounds": 1500, "current_round": 861,
        "version": "v2.41.212", "updated": "2026-11-04", "custom": {"keep": True}}


def test_updates_round_version_theme_next(hub_root):
    path = _card(hub_root, "某项目", dict(BASE))
    content = ("目的：R862 自动维护续跑点。做了什么：改 brain.py。"
               "下一步方向：B080 会话补扫对齐；B081 卡片自动创建。"
               "验证结果：单测。如何回滚：git revert。")
    upd = brain.sync_state_card(hub_root, "某项目", content, agent="workbuddy",
                                record_id=7, date="2026-11-04")
    assert upd["round"] == 862, upd
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == "v2.41.212"
    # 版本留在正文里才更新
    upd2 = brain.sync_state_card(hub_root, "某项目", "本轮 v2.41.213 收口", agent="workbuddy")
    assert upd2["version"] == "v2.41.213", upd2
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["current_round"] == 862
    assert data["custom"] == {"keep": True}, "未知字段不许被自动同步吃掉"
    assert data["last_round"]["record_id"] == 7
    assert data["last_round"]["next_candidates"] == ["B080 会话补扫对齐", "B081 卡片自动创建"], data
    assert (path.parent / (path.name + ".bak-auto")).is_file(), "写前要留一份可回滚备份"


def test_target_round_is_not_progress(hub_root):
    path = _card(hub_root, "某项目", dict(BASE))
    brain.sync_state_card(hub_root, "某项目",
                          "目的：R862 收口。总目标是 R1500，本轮不碰。", agent="workbuddy")
    assert json.loads(path.read_text(encoding="utf-8"))["current_round"] == 862


def test_round_never_regresses(hub_root):
    path = _card(hub_root, "某项目", dict(BASE, current_round=900))
    upd = brain.sync_state_card(hub_root, "某项目", "回顾 R862 的结论", agent="workbuddy")
    assert upd == {}, "旧轮次正文不许把进度改回去"
    assert json.loads(path.read_text(encoding="utf-8"))["current_round"] == 900


def test_no_card_no_creation(hub_root):
    out = Path(hub_root) / "普通项目"
    out.mkdir(parents=True)
    upd = brain.sync_state_card(hub_root, "普通项目", "目的：R862 随便说说", agent="workbuddy")
    assert upd == {}
    assert not (out / "output" / "迭代状态.json").exists(), "不许给普通项目凭空造卡片"


def test_broken_json_untouched(hub_root):
    out = Path(hub_root) / "坏卡项目" / "output"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "迭代状态.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    assert brain.sync_state_card(hub_root, "坏卡项目", "R862 搞不定", agent="workbuddy") == {}
    assert path.read_text(encoding="utf-8") == "{ 这不是 json"


def test_log_work_wires_it(hub_root):
    """真入口（hub_log_work）也要接上，否则 sync 再对也永远没人调。"""
    path = _card(hub_root, "迭代项目", dict(BASE))
    # 与 test_state_card_brief_r838._project 同形：真入口会自己开表，夹具得先把库建出来
    brain.init_db(hub_root)
    brain.add_record(hub_root, "迭代项目", "workbuddy", "2026-11-04", "占位", "占位")
    out = agenthub_mcp.call_tool("hub_log_work", {
        "project": "迭代项目", "agent": "workbuddy",
        "content": ("目的：R862 把续跑点接进写入口。做了什么：改 agenthub_mcp.py。"
                    "下一步方向：B080 对齐。验证结果：单测。如何回滚：git revert。")},
        hub_root)
    assert "续跑点已自动更新" in out and "R862" in out, out[:300]
    assert json.loads(path.read_text(encoding="utf-8"))["current_round"] == 862
