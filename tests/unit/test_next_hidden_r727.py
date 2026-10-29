# -*- coding: utf-8 -*-
"""R727：`next` 不能把「被过滤掉」报成「没活干」。

修之前会怎样（两种真实场景，症状同一条）：
① agent 处于 draining/paused/offline 时，brain 把 ready/takeover 清空（R36 的收尾闸门），
   CLI 落到「前沿队列为空（没有待办/在办任务）」——用户去看板确认明明有 12 条待办，
   于是怀疑大脑读数错了，实际是他自己的 agent 状态挡住了。
② agent 声明过能力（R37 技能筛），库里全是它接不住的活 → 同样一句「没活干」。
两种情况 `counts` 也是 0（过滤后才算数），所以 `--json` 的消费者同样看不出原因。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402


@pytest.fixture
def db(hub_root):
    import brain
    brain.init_db(hub_root)
    return hub_root


def _json(capsys):
    out = capsys.readouterr().out.strip()
    assert out.startswith("{") and out.endswith("}"), out
    return json.loads(out)


def test_control_state_filter_is_visible_in_json(db, capsys):
    import brain
    brain.task_add(db, "收尾期该看不见的活", project="状态项目")
    brain.agent_control_set(db, "zcode", "draining", by="zcode")
    assert cli.main(["next", "--agent", "zcode", "--project", "状态项目", "--json"]) == 0
    d = _json(capsys)
    assert d["counts"]["ready"] == 0 and d["ready"] == []
    # 原因必须进 JSON：谁在挡、挡了几条
    assert d["hidden"]["control"] == 1 and d["hidden"]["skills"] == 0
    assert d["control"]["state"] == "draining"


def test_skill_filter_is_visible_in_json(db, capsys):
    import brain
    brain.task_add(db, "要 OCR 的活", project="技能项目", skills="ocr")
    brain.agent_skills_set(db, "buddy", "gui,plot", by="buddy")
    assert cli.main(["next", "--agent", "buddy", "--project", "技能项目", "--json"]) == 0
    d = _json(capsys)
    assert d["counts"]["ready"] == 0
    assert d["hidden"]["skills"] == 1 and d["hidden"]["control"] == 0
    assert d["skills"] == ["gui", "plot"]


def test_text_mode_names_the_real_reason(db, capsys):
    import brain
    brain.task_add(db, "收尾期看不见的活", project="状态项目")
    brain.agent_control_set(db, "zcode", "draining", by="zcode")
    assert cli.main(["next", "--agent", "zcode", "--project", "状态项目"]) == 0
    out = capsys.readouterr().out
    assert "前沿队列为空" not in out
    assert "收尾" in out and "1 条" in out
    assert 'state="active"' in out  # 给出恢复出口，不只是「被挡了」


def test_text_mode_skill_reason(db, capsys):
    import brain
    brain.task_add(db, "要 OCR 的活", project="技能项目", skills="ocr")
    brain.agent_skills_set(db, "buddy", "gui,plot", by="buddy")
    assert cli.main(["next", "--agent", "buddy", "--project", "技能项目"]) == 0
    out = capsys.readouterr().out
    assert "前沿队列为空" not in out
    assert "能力声明" in out and "ocr" in out


def test_no_filter_no_noise(db, capsys):
    """没被挡时不许凭空多一段解释——文案只在真的藏了东西时出现。"""
    import brain
    brain.task_add(db, "正常的活", project="干净项目")
    assert cli.main(["next", "--agent", "zcode", "--project", "干净项目"]) == 0
    out = capsys.readouterr().out
    assert "能力声明" not in out and "state=\"active\"" not in out
    assert "正常的活" in out


def test_empty_brain_still_says_empty(db, capsys):
    """库里真没活时保持原话，不能被新文案冒充成「被挡了」。"""
    assert cli.main(["next", "--agent", "zcode"]) == 0
    out = capsys.readouterr().out
    assert "前沿队列为空" in out and "挡住" not in out


# ---------------------------------------------------------------- MCP 面
# hub_next 是 agent 实际在用的那一面：只修 CLI 等于没修。

def test_hub_next_names_the_skill_filter(db):
    import agenthub_mcp as m
    import brain
    brain.task_add(db, "要 OCR 的活", project="技能项目", skills="ocr")
    brain.agent_skills_set(db, "buddy", "gui,plot", by="buddy")
    out = m.call_tool("hub_next", {"agent": "buddy", "project": "技能项目"}, db)
    assert "前沿队列为空" not in out, out
    assert "能力声明" in out and "ocr" in out and "gui、plot" in out
    assert 'hub_agent(action="skills"' in out  # 出口要给全，不能只说「被筛了」


def test_hub_next_control_line_keeps_phrase_and_adds_count(db):
    """R36 的「已停止推荐新任务」是既有契约（adv_mcp 盯），只能补信息不能换措辞。"""
    import agenthub_mcp as m
    import brain
    brain.task_add(db, "收尾期挡住的活", project="状态项目")
    brain.agent_control_set(db, "zcode", "draining", by="zcode")
    out = m.call_tool("hub_next", {"agent": "zcode", "project": "状态项目"}, db)
    assert "已停止推荐新任务" in out, out
    assert "1 条被挡在外面" in out, out


def test_hub_next_empty_frontier_wording_unchanged(db):
    import agenthub_mcp as m
    out = m.call_tool("hub_next", {}, db)
    assert "前沿队列为空（没有待办/在办任务）。" in out, out
    assert "能力声明" not in out
