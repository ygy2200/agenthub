# -*- coding: utf-8 -*-
"""简报采纳度 R813：送达 ≠ 用上，usage_health 要能指出「有简报却零回忆」的 agent。

背景：主人反复说「不提醒就不读记录和记忆」。R811/R812 把简报塞进会话只是第一步，
若 agent 收到简报仍不查记忆，问题在送达渠道而非意愿——没有度量就只能再猜。
本文件钉住 usage_health 的简报采纳信息行：分渠道计数、点名冷 agent、无简报时不刷行。
"""
import datetime

import brain
import ops


def _seed(root):
    brain.init_db(root)
    brain.add_record(root, "演示-采纳", "qoder", "2026-10-30", "演示记录", "内容")


def _journal(root, agent, target):
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    with brain.db_conn(root) as conn:
        brain._journal_append(conn, ts, agent, "brain_brief", target, "", "x chars")


def _detail(root):
    r = ops.run_check("usage_health", "", str(root))
    return r, " ".join(r.get("detail") or [])


def test_brief_line_counts_channels_and_names_cold_agents(hub_root):
    _seed(hub_root)
    _journal(hub_root, "qoder", "initialize")
    _journal(hub_root, "workbuddy", "session")
    _journal(hub_root, "workbuddy", "session")
    r, text = _detail(hub_root)
    assert "简报采纳" in text, text
    assert "钩子 2" in text and "握手 1" in text, text
    assert "qoder" in text and "workbuddy" in text, "收到简报的 agent 都应被点名"
    assert "零记忆/检索调用" in text, "没有任何回忆调用时必须点明"
    # hub_root 是临时库，但接入状态读的是本机真实配置，闲置项会让 ok=False——与本行无关；
    # 简报采纳只做信息行，绝不能出现在失败摘要里。
    assert "简报" not in (r.get("summary") or ""), "简报采纳只是信息行，不参与健康判定"


def test_no_brief_rows_means_no_noise(hub_root):
    _seed(hub_root)
    r, text = _detail(hub_root)
    assert "简报采纳" not in text, f"没有简报送达就不该出现采纳行：{text}"
