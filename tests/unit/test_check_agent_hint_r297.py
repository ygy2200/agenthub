# -*- coding: utf-8 -*-
r"""拒收未登记 agent 时必须给出登记出口（R297）。

新脑上没有本机主力 agent（如 qoder）的登记，只报「可用：…」会让人以为无解。
错误信息补上 <root>/config.agents.json 的登记入口与 unknown 兜底。
"""
import brain


def test_unknown_agent_error_points_to_registry_file(hub_root):
    brain.init_db(hub_root)
    key, err = brain.check_agent("qoder", hub_root)
    assert key == ""
    assert "不在注册表内" in err and "qoder" in err, err
    assert "config.agents.json" in err, err
    assert str(hub_root) in err, err
    assert "unknown" in err, err


def test_registered_and_anonymous_agents_pass(hub_root):
    brain.init_db(hub_root)
    assert brain.check_agent("zcode", hub_root) == ("zcode", "")
    assert brain.check_agent("unknown", hub_root) == ("unknown", "")
    assert brain.check_agent("", hub_root) == ("", "")


def test_no_root_has_no_file_hint(hub_root):
    brain.init_db(hub_root)
    _, err = brain.check_agent("qoder", "")
    assert "不在注册表内" in err, err
    assert "config.agents.json" not in err, err
