# -*- coding: utf-8 -*-
"""R931：生产签名保守规范化 mojibake；存量只迁可证明的一代，独立留证可回滚。"""
import importlib.util
from pathlib import Path

import brain
import pytest

NORMAL = "RuntimeError: '流量看板.lnk' 更新成功，流量看板.lnk 读回同一路径 ascii"
MOJIBAKE = "RuntimeError: '娴侀噺鐪嬫澘.lnk' 更新成功，流量看板.lnk 读回同一路径 ascii"
LEGACY_SHAPED = ("KeyError: '娴侀噺鐪嬫澘.lnk' 检查 layout_stress setstylesheet "
                 "test_contrast window.py 四个页面刷底")


@pytest.fixture(scope="module")
def tool():
    path = Path(__file__).resolve().parents[2] / "tools" / "error_sig_mojibake_r931.py"
    spec = importlib.util.spec_from_file_location("error_sig_mojibake_r931", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _add(root, detail, title):
    info = brain.error_add(root, "qoder", title, detail=detail, project="p",
                           return_info=True)
    return int(info["id"])


def _raw_terms(detail, title="取证"):
    return brain._error_sig_terms(detail, title, normalize_mojibake=False)


def _seed_old(root, detail, title, ruler="weighted"):
    eid = _add(root, detail, title)
    terms = _raw_terms(detail, title)
    old = (brain._error_sig_key(terms) if ruler == "weighted"
           else brain._error_sig_key_legacy(terms))
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE errors SET sig=? WHERE id=?", (old, eid))
    return eid, old


def _row(root, eid):
    with brain.db_conn(root) as conn:
        return dict(conn.execute(
            "SELECT sig, pre_sig, pre_sig_r930 FROM errors WHERE id=?", (eid,)).fetchone())


def test_production_cp936_mojibake_and_normal_text_share_one_key():
    normal = brain.error_sig_of(NORMAL)
    repaired = brain.error_sig_of(MOJIBAKE)
    assert normal and repaired == normal
    assert "流量看板.lnk" in repaired
    assert "娴侀噺鐪嬫澘.lnk" not in repaired


def test_production_latin1_repair_happens_before_lowercase():
    normal = brain.error_sig_of("RuntimeError: 'café' update failed")
    repaired = brain.error_sig_of("RuntimeError: 'cafÃ©' update failed")
    assert normal and repaired == normal
    assert "café" in repaired


@pytest.mark.parametrize("text", [
    "RuntimeError: '流量看板' 页面是否真的刷了底",
    "RuntimeError: 'café' update failed",
    "RuntimeError: 'This text is fine already :þ'",
    "RuntimeError: 'abc�def'",
    "RuntimeError: 'Ã©'",
])
def test_normal_or_low_confidence_text_is_not_rewritten(text):
    normalized = brain.error_signature_terms(text)
    raw = brain.error_signature_terms(text, normalize_mojibake=False)
    assert normalized == raw


def test_ambiguous_repairs_are_never_guessed(monkeypatch):
    monkeypatch.setattr(brain, "_mojibake_strict_roundtrip", lambda text, enc: {
        "cp936": "流量看板", "latin1": "café",
    }.get(enc))
    assert brain.choose_mojibake_repair("whatever") is None


def test_r929_resign_is_frozen_on_unnormalized_terms(hub_root):
    brain.init_db(hub_root)
    eid, old = _seed_old(hub_root, MOJIBAKE, "R929旧键")
    got = brain.resign_error_sigs(hub_root, apply=True)
    assert got["same"] == 1 and got["unreproducible"] == 0, got
    assert _row(hub_root, eid)["sig"] == old


def test_migration_accepts_r929_weighted_key_and_keeps_its_own_evidence(hub_root):
    brain.init_db(hub_root)
    eid, old = _seed_old(hub_root, MOJIBAKE, "R929权重键")
    got = brain.resign_mojibake_error_sigs(hub_root, apply=True)
    assert got["changed"] == 1 and got["resigned"] == 1, got
    row = _row(hub_root, eid)
    assert row["sig"] == brain.error_sig_of(NORMAL)
    assert row["pre_sig_r930"] == old
    assert row["pre_sig"] == "", "不得覆盖 R929 的留证列"


def test_migration_accepts_pre_r929_legacy_key(hub_root):
    brain.init_db(hub_root)
    eid, old = _seed_old(hub_root, LEGACY_SHAPED, "更早legacy键", ruler="legacy")
    assert old != brain._error_sig_key(_raw_terms(LEGACY_SHAPED))
    got = brain.resign_mojibake_error_sigs(hub_root, apply=True)
    assert got["changed"] == 1 and got["resigned"] == 1, got
    row = _row(hub_root, eid)
    assert row["pre_sig_r930"] == old
    assert "流量看板.lnk" in row["sig"]


def test_unreproducible_and_empty_signatures_are_not_written(hub_root):
    brain.init_db(hub_root)
    junk_id, _ = _seed_old(hub_root, MOJIBAKE, "不可复现")
    empty_id, _ = _seed_old(hub_root, MOJIBAKE, "空键")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE errors SET sig='人工改过的键' WHERE id=?", (junk_id,))
        conn.execute("UPDATE errors SET sig='' WHERE id=?", (empty_id,))
    got = brain.resign_mojibake_error_sigs(hub_root, apply=True)
    assert got["unreproducible"] == 1 and got["empty"] == 1, got
    assert got["resigned"] == 0
    assert _row(hub_root, junk_id)["sig"] == "人工改过的键"
    assert _row(hub_root, empty_id)["sig"] == ""


def test_dry_run_limit_and_second_apply_are_noop(hub_root):
    brain.init_db(hub_root)
    first, old = _seed_old(hub_root, MOJIBAKE, " dry-run一")
    second, _ = _seed_old(hub_root, MOJIBAKE + " 第二次", "dry-run二")
    dry = brain.resign_mojibake_error_sigs(hub_root)
    assert dry["changed"] == 2 and dry["resigned"] == 0
    assert _row(hub_root, first)["sig"] == old
    limited = brain.resign_mojibake_error_sigs(hub_root, apply=True, limit=1)
    assert limited["changed"] == 2 and limited["resigned"] == 1
    again = brain.resign_mojibake_error_sigs(hub_root, apply=True)
    assert again["resigned"] == 1, "只补 limit 留下的第二行"
    final = brain.resign_mojibake_error_sigs(hub_root, apply=True)
    assert final["changed"] == 0 and final["resigned"] == 0
    assert "流量看板.lnk" in _row(hub_root, second)["sig"]


def test_dry_run_does_not_even_add_the_evidence_column(hub_root):
    brain.init_db(hub_root)
    _seed_old(hub_root, MOJIBAKE, "预演不迁列")
    with brain.db_conn(hub_root) as conn:
        conn.execute("ALTER TABLE errors DROP COLUMN pre_sig_r930")
    got = brain.resign_mojibake_error_sigs(hub_root)
    assert got["changed"] == 1 and got["resigned"] == 0, got
    with brain.db_conn(hub_root) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(errors)")}
    assert "pre_sig_r930" not in cols


def test_legacy_db_gets_independent_column_without_losing_r929_marker(hub_root):
    brain.init_db(hub_root)
    eid, old = _seed_old(hub_root, MOJIBAKE, "旧库迁列")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE errors SET pre_sig='r929留证' WHERE id=?", (eid,))
        conn.execute("ALTER TABLE errors DROP COLUMN pre_sig_r930")
    got = brain.resign_mojibake_error_sigs(hub_root, apply=True)
    assert got["resigned"] == 1, got
    row = _row(hub_root, eid)
    assert row["pre_sig"] == "r929留证"
    assert row["pre_sig_r930"] == old


def test_real_tool_undo_restores_exactly_one_generation(hub_root, tool):
    brain.init_db(hub_root)
    eid, old = _seed_old(hub_root, MOJIBAKE, "真实工具回滚")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE errors SET pre_sig='r929留证' WHERE id=?", (eid,))
    brain.resign_mojibake_error_sigs(hub_root, apply=True)
    migrated = _row(hub_root, eid)["sig"]
    assert migrated != old
    tool.undo(hub_root)
    row = _row(hub_root, eid)
    assert row["sig"] == old
    assert row["pre_sig"] == "r929留证"
    assert row["pre_sig_r930"] == ""


def test_real_tool_backup_is_a_consistent_database(hub_root, tool):
    brain.init_db(hub_root)
    _seed_old(hub_root, MOJIBAKE, "备份完整性")
    path = Path(tool.backup(hub_root))
    assert path.exists() and path.name.startswith("brain-pre-mojibake-r931-")
    import sqlite3
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM errors").fetchone()[0] == 1
    finally:
        conn.close()
