# -*- coding: utf-8 -*-
"""R1037：doctor 首屏要能直接照做——检查项自带的 fix_brief 不许被长口径覆盖。

现场：`adherence` 的修复口径有两屏（三类成因 + 补扫钩子），doctor 默认屏整段打印，
读完还是不知道该敲哪条命令。检查项侧写了 `fix_brief`，但聚合时被 `issue["fix_brief"] = r["fix"]`
无条件覆盖——这一层等于架空。
"""
import pathlib

import brain
import ops


def test_fix_brief_wins_over_fix(tmp_path, monkeypatch):
    root = str(tmp_path / "hub")
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    brain.init_db(root)
    monkeypatch.setattr(ops, "run_check", lambda *a, **k: {
        "ok": False, "summary": "演示", "fix": "很长很长的三类成因说明",
        "fix_brief": "先跑这一条命令", "detail": []})
    rep = ops.run_patrol(root, checks=["dup_mem"])
    assert rep["issues"], rep
    assert rep["issues"][0]["fix_brief"] == "先跑这一条命令", rep["issues"][0]


def test_adherence_declares_brief():
    src = (pathlib.Path(__file__).resolve().parents[2] / "ops.py").read_text(encoding="utf-8")
    assert '"fix_brief": "先 `python cli.py adherence-backfill`' in src


def test_fix_briefs_table_covers_all_current_reds(tmp_path, monkeypatch):
    """R1039：当前会红的五项都要有首屏动作（表格驱动，防止新增检查项时忘了给）。"""
    for name in ("backup_health", "usage_health", "ghost_memory"):
        assert name in ops.FIX_BRIEFS, f"{name} 没有首屏动作"
        assert len(ops.FIX_BRIEFS[name]) <= 90, f"{name} 的首屏动作太长：>90 字"


def test_fix_briefs_table_covers_all_current_reds(tmp_path, monkeypatch):
    """R1039：当前会红的五项都要有首屏动作（表格驱动，防止新增检查项时忘了给）。"""
    for name in ("backup_health", "usage_health", "ghost_memory"):
        assert name in ops.FIX_BRIEFS, f"{name} 没有首屏动作"
        assert len(ops.FIX_BRIEFS[name]) <= 90, f"{name} 的首屏动作太长：>90 字"
