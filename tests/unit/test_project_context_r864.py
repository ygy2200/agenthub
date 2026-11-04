# -*- coding: utf-8 -*-
"""R864：项目上下文不只看 cwd——开口点名项目就带上项目近况。

病根：`infer_project_from_cwd` 只认「cwd 位于大脑根下的项目目录」。Desktop 会话的
cwd 常在远程会话目录/根目录，于是简报没有「当前项目」、即时回忆也不带项目近况——
用户说「继续」时模型仍得自己翻台账。本轮的补法都不靠猜：项目名来自已登记表，
最近活跃来自最近三天的记录；推不出来就维持原样（宁可没有，不可认错）。
"""
import datetime
from pathlib import Path

import brain


def _project(root, name, date, title="进度", agent="qoder"):
    brain.init_db(root)
    brain.add_record(root, name, agent, date, f"{date}（{agent}）· {title}",
                     f"{date}（{agent}）· {title}")


def _today(offset=0):
    return (datetime.date.today() - datetime.timedelta(days=offset)).isoformat()


def test_project_mentioned_full_name_and_alias(hub_root):
    _project(hub_root, "AgentHub-多agent文件统一管理", _today())
    _project(hub_root, "红色沙漠-Mod安装", _today(3))
    assert brain.project_mentioned(hub_root, "继续 AgentHub 的迭代") == \
        "AgentHub-多agent文件统一管理"
    assert brain.project_mentioned(hub_root, "红色沙漠又崩了") == "红色沙漠-Mod安装"
    assert brain.project_mentioned(hub_root, "今天天气不错") == ""


def test_project_mentioned_ambiguous_alias_picks_recent(hub_root):
    _project(hub_root, "AgentHub-技能原生化", _today(9), "老项目")
    _project(hub_root, "AgentHub-多agent文件统一管理", _today(0), "新项目")
    assert brain.project_mentioned(hub_root, "AgentHub 现在怎么样") == \
        "AgentHub-多agent文件统一管理"


def test_recent_active_project_window(hub_root):
    _project(hub_root, "新鲜项目", _today(1))
    assert brain.recent_active_project(hub_root) == "新鲜项目"
    _project(hub_root, "陈旧项目", _today(10))
    # 新记录的项目仍然优先；窗口外的不会被选中
    assert brain.recent_active_project(hub_root) == "新鲜项目"


def test_recent_active_project_prefers_own_agent(hub_root):
    """重启后接着干的是「我上次那个项目」，不是别人五分钟前写的项目。"""
    _project(hub_root, "我的项目", _today(1), "我昨天干到一半", agent="qoder")
    _project(hub_root, "别人的项目", _today(), "别人刚写", agent="workbuddy")
    assert brain.recent_active_project(hub_root) == "别人的项目"
    assert brain.recent_active_project(hub_root, agent="qoder") == "我的项目"
    res = brain.session_brief(hub_root, agent="qoder", cwd=hub_root)
    assert res["project"] == "我的项目", res


def test_brief_falls_back_to_recent_project(hub_root):
    _project(hub_root, "演示-续跑", _today(), "做到一半")
    card = Path(hub_root) / "演示-续跑" / "output" / "迭代状态.json"
    card.parent.mkdir(parents=True)
    card.write_text('{"current_round": 12, "version": "v0.0.12",'
                    ' "last_round": {"theme": "修导出"},'
                    ' "next_candidates": ["B002 写测试"]}', encoding="utf-8")
    res = brain.session_brief(hub_root, agent="qoder", cwd=hub_root)
    text = res["text"]
    assert res["project"] == "演示-续跑", res
    assert "最近活跃项目（cwd 未落在项目目录，按最近记录推断）：演示-续跑" in text, text
    assert "续跑点" in text and "v0.0.12" in text, text


def test_brief_stale_project_not_used(hub_root):
    _project(hub_root, "十天前的项目", _today(10))
    res = brain.session_brief(hub_root, agent="qoder", cwd=hub_root)
    assert res["project"] == ""
    assert "当前项目" not in res["text"] and "最近活跃项目" not in res["text"], res["text"]


def test_prompt_mention_brings_project_block(hub_root, tmp_path):
    _project(hub_root, "AgentHub-多agent文件统一管理", _today(), "续跑点自动同步")
    res = brain.prompt_recall(hub_root, "AgentHub 的续跑点现在准了吗", agent="qoder",
                              cwd=hub_root, session="m1",
                              state_path=str(tmp_path / "m1.json"))
    assert res["project"] == "AgentHub-多agent文件统一管理", res
    assert "本项目近况" in res["text"], res["text"]


def test_prompt_mention_beats_cwd_project(hub_root, tmp_path):
    _project(hub_root, "项目甲", _today(), "甲在干活")
    _project(hub_root, "项目乙", _today(1), "乙有发布脚本")
    brain.add_memory(hub_root, "项目乙：发布脚本要先跑门禁再打 exe。",
                     kind="lesson", tags="发布脚本,exe", agent="qoder", project="项目乙")
    res = brain.prompt_recall(hub_root, "项目乙 的 发布脚本 exe 怎么打", agent="qoder",
                              cwd=str(Path(hub_root) / "项目甲"), session="m2",
                              state_path=str(tmp_path / "m2.json"))
    assert res["project"] == "项目乙", res
    assert "本项目近况" in res["text"], res["text"]
