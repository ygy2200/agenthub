# -*- coding: utf-8 -*-
"""R838：简报里补「续跑点」——干到哪了不该靠人提醒。

为什么改：轮次、版本、下一批候选只躺在 `output/迭代状态.json` 里，每个新会话都得
先手动翻台账才敢动手；翻不到就只能重新盘点（这次开工真的又盘了一遍）。简报的职责
本来就是「我是谁、现在在哪、下一步」，缺这一段，模型只能靠用户提醒。

修法：`project_state_card()` 读 `<项目>/output/迭代状态.json`（识别不到就退回
`state.json`），`session_brief` 在「当前项目」段后面加一块「续跑点」。约定优于配置：
只认 round/current_round、version、next_candidates、last_round.theme、updated，其余忽略。

本文件钉五件事：
  1. 状态卡在位时，简报里出现轮次 / 版本 / 下一批；
  2. 没有状态卡 → 不出现这一段（不占版面说废话）；
  3. 坏 JSON → 简报照常生成（fail-open 是简报的底线）；
  4. `next_candidates` 最多列 3 条（简报是索引不是清单）；
  5. 没有可用字段的卡 → 整段不出。

「反证」：每个用例都先给项目写一条记录，让「当前项目」这段真的出现——否则断言
「没有续跑点」在旧代码上也绿，测的就不是这道闸，而是「项目段压根没渲染」。

「修之前会怎样失败」：第 1 条在旧代码上必红——旧简报只有「当前项目 N 条活跃记录」，
没有轮次这个概念。
"""
import json
from pathlib import Path

import brain


def _project(root, name):
    """让项目在库里存在（简报只对已登记项目渲染「当前项目」段）。"""
    brain.init_db(root)
    brain.add_record(root, name, "workbuddy", "2026-11-02", f"{name} 开工记录",
                     "本轮只用于让项目段出现。")


def _card(root, project, payload, name="迭代状态.json"):
    out = Path(root) / project / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


PAYLOAD = {
    "schema": 1, "current_round": 835, "version": "v2.41.190", "updated": "2026-11-02",
    "last_round": {"theme": "把采纳正信号接进开工推送排序", "agent": "zcode"},
    "next_candidates": ["B043 单会话注入预算（P1）", "B041 签名沉淀（P1）",
                        "B044 发布链点名（P1）", "B046 残缺词元（P2）"],
}


def test_card_shows_round_and_next(hub_root):
    _project(hub_root, "某项目")
    _card(hub_root, "某项目", PAYLOAD)
    text = brain.session_brief(hub_root, agent="workbuddy", project="某项目")["text"]
    assert "当前项目：某项目" in text, "前提不成立：项目段没渲染，后面的断言没有意义"
    assert "续跑点" in text, text[:400]
    assert "R835" in text and "v2.41.190" in text, text[:400]
    assert "B043" in text and "B041" in text, text[:400]
    assert "B046" not in text, "next_candidates 只列前 3 条（简报是索引不是清单）"


def test_no_card_no_section(hub_root):
    _project(hub_root, "无卡项目")
    text = brain.session_brief(hub_root, agent="workbuddy", project="无卡项目")["text"]
    assert "当前项目：无卡项目" in text, "前提不成立：项目段没渲染"
    assert "续跑点" not in text


def test_broken_json_fail_open(hub_root):
    _project(hub_root, "坏卡项目")
    out = Path(hub_root) / "坏卡项目" / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "迭代状态.json").write_text("{ 这不是 json", encoding="utf-8")
    brief = brain.session_brief(hub_root, agent="workbuddy", project="坏卡项目")
    assert brief["text"], "坏状态卡不能把整篇简报带崩"
    assert "当前项目：坏卡项目" in brief["text"], "前提不成立"
    assert "续跑点" not in brief["text"]
    assert brain.project_state_card(hub_root, "坏卡项目") == {}


def test_empty_card_no_section(hub_root):
    _project(hub_root, "空卡项目")
    _card(hub_root, "空卡项目", {"schema": 1})
    text = brain.session_brief(hub_root, agent="workbuddy", project="空卡项目")["text"]
    assert "当前项目：空卡项目" in text, "前提不成立"
    assert "续跑点" not in text, "没有可用字段就整段不出"


def test_fallback_state_json(hub_root):
    _project(hub_root, "退化项目")
    _card(hub_root, "退化项目", {"round": 42, "version": "v0.1"}, name="state.json")
    card = brain.project_state_card(hub_root, "退化项目")
    assert card["round"] == "42" and card["version"] == "v0.1", card


def test_next_candidates_nested_in_last_round(hub_root):
    """本机台账把候选挂在 last_round 里——两种写法都得认，否则线上永远读不到「下一批」。"""
    _project(hub_root, "嵌套项目")
    _card(hub_root, "嵌套项目", {"current_round": 9, "version": "v0.9",
                               "last_round": {"theme": "x", "next_candidates": ["B001 甲"]}})
    assert brain.project_state_card(hub_root, "嵌套项目")["next"] == ["B001 甲"]
