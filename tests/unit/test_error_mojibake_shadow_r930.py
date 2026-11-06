# -*- coding: utf-8 -*-
"""R930：mojibake 只影子评估，不直接改签名/写库。"""
import importlib.util
from pathlib import Path

import brain
import pytest


@pytest.fixture(scope="module")
def tool():
    path = Path(__file__).resolve().parents[2] / "tools" / "error_mojibake_shadow_r930.py"
    spec = importlib.util.spec_from_file_location("error_mojibake_shadow_r930", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cp936_mojibake_has_one_strict_repair(tool):
    got = tool.choose_repair("娴侀噺鐪嬫澘.lnk")
    assert got is not None
    assert got["encoding"] == "cp936"
    assert got["repaired"] == "流量看板.lnk"
    assert got["roundtrip"] is True


def test_latin1_mojibake_positive_examples(tool):
    assert tool.choose_repair("æµéçæ¿.lnk")["repaired"] == "流量看板.lnk"
    assert tool.choose_repair("cafÃ©")["repaired"] == "café"


@pytest.mark.parametrize("text", [
    "流量看板",
    "中文错误",
    "café",
    "This text is fine already :þ",
    "abc�def",
])
def test_normal_or_already_damaged_text_is_not_repaired(tool, text):
    assert tool.choose_repair(text) is None


def test_isolated_latin1_fragment_has_insufficient_evidence(tool):
    candidates = tool.assess_term("Ã©")
    assert candidates, "影子层可以报告严格可逆候选"
    assert tool.choose_repair("Ã©") is None, "但证据不足时不许自动选它"


def test_multiple_plausible_candidates_are_not_guessed(tool, monkeypatch):
    monkeypatch.setattr(tool, "_strict_roundtrip", lambda text, enc: {
        "cp936": "流量看板",
        "latin1": "café",
    }.get(enc))
    assert tool.choose_repair("whatever") is None


def _add_error(root, detail, title="快捷方式回读失败"):
    info = brain.error_add(root, "qoder", title, detail=detail,
                           project="p", return_info=True)
    return int(info["id"])


def test_shadow_scan_reports_hypothetical_change_without_writing(hub_root, tool):
    brain.init_db(hub_root)
    eid = _add_error(hub_root,
                     "RuntimeError: '娴侀噺鐪嬫澘.lnk' 更新成功，流量看板.lnk 读回同一路径")
    with brain.db_conn(hub_root) as conn:
        before = conn.execute("SELECT sig FROM errors WHERE id=?", (eid,)).fetchone()["sig"]
        journal_before = conn.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"]

    got = tool.scan(hub_root)

    assert got["rows_with_candidates"] == 1, got
    assert got["hypothetical"], got
    row = next(x for x in got["hypothetical"] if x["id"] == eid)
    assert "娴侀噺鐪嬫澘.lnk" in row["before"]
    assert "流量看板.lnk" in row["after"]
    assert "娴侀噺鐪嬫澘.lnk" not in row["after"]
    with brain.db_conn(hub_root) as conn:
        after = conn.execute("SELECT sig FROM errors WHERE id=?", (eid,)).fetchone()["sig"]
        journal_after = conn.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"]
    assert after == before
    assert journal_after == journal_before


def test_normal_and_mojibake_inputs_converge_to_the_same_hypothetical_key(hub_root, tool):
    brain.init_db(hub_root)
    normal_id = _add_error(
        hub_root, "RuntimeError: '流量看板.lnk' 更新成功，流量看板.lnk 读回同一路径 ascii",
        title="正常中文快捷方式")
    mojibake_id = _add_error(
        hub_root, "RuntimeError: '娴侀噺鐪嬫澘.lnk' 更新成功，流量看板.lnk 读回同一路径 ascii",
        title="乱码快捷方式")
    got = tool.scan(hub_root)
    changed = next(x for x in got["hypothetical"] if x["id"] == mojibake_id)
    with brain.db_conn(hub_root) as conn:
        normal_sig = conn.execute(
            "SELECT sig FROM errors WHERE id=?", (normal_id,)).fetchone()["sig"]
    assert changed["after"] == normal_sig


def test_shadow_scan_leaves_normal_chinese_signature_alone(hub_root, tool):
    brain.init_db(hub_root)
    eid = _add_error(hub_root,
                     "RuntimeError: '流量看板.lnk' 更新成功，页面是否真的刷了底")
    with brain.db_conn(hub_root) as conn:
        before = conn.execute("SELECT sig FROM errors WHERE id=?", (eid,)).fetchone()["sig"]
    got = tool.scan(hub_root)
    assert got["rows_with_candidates"] == 0, got
    assert got["hypothetical"] == [], got
    with brain.db_conn(hub_root) as conn:
        assert conn.execute("SELECT sig FROM errors WHERE id=?", (eid,)).fetchone()["sig"] == before
