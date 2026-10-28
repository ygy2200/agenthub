# -*- coding: utf-8 -*-
r"""整洁体检（hygiene）判定口径（R253）。

背景（本机真实数据，2026-10-28）：hygiene 报红「备份文件 146 个已明显堆积」，
但按当时策略 `core.BACKUP_KEEP=30` 每文件都没越界——四个 agent 的引导文件各自躺满
30 份（claude/CLAUDE.md 30、dsh/AGENTS.md 30、hermes/SOUL.md 34、hermes/config.yaml 23），
合计 146 越过「总数 ≥100」这条硬阈值。**合规即红**，检查项从此常年报红。

本文件守 R253 的两处改动：
  ① `core.BACKUP_KEEP` 30 → 8，且 `_prune_backups` 只清自产备份、绝不动人工命名备份；
  ② hygiene 改按**原文件保留策略**判红：自产备份 > keep 才红，人工命名只统计不判红；
     并且 `dirs` 可注入——否则这套判定只能用真机目录验证（等于不可测）。
"""
from pathlib import Path

import brain
import core
import ops


def _mk(d: Path, name: str, n: int) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"{name}.bak-agenthub-20260101-0000{i:02d}-000").write_text(f"v{i}", encoding="utf-8")


def test_compliant_state_is_green(hub_root, tmp_path):
    """① 每个文件都不超 keep：即便总数远超旧的 100 阈值，也必须判 ok。"""
    brain.init_db(hub_root)
    d = tmp_path / "agents"
    for name in ("CLAUDE.md", "AGENTS.md", "SOUL.md", "config.yaml", "settings.json"):
        _mk(d, name, core.BACKUP_KEEP)          # 5 文件 × 8 = 40 份，全部合规
    r = ops._hygiene(hub_root, dirs=[("agents", d)])
    assert r["ok"] is True, r
    assert "策略内" in r["summary"], r["summary"]


def test_over_limit_group_is_red(hub_root, tmp_path):
    """② 单文件超出 keep 份 → 红，且点名是哪个文件、多几份。"""
    brain.init_db(hub_root)
    d = tmp_path / "agents"
    _mk(d, "SOUL.md", core.BACKUP_KEEP + 6)
    _mk(d, "CLAUDE.md", 2)
    r = ops._hygiene(hub_root, dirs=[("agents", d)])
    assert r["ok"] is False, r
    assert "超出保留上限" in r["detail"][0], r["detail"]
    assert "SOUL.md" in r["detail"][0] and "多 6" in r["detail"][0], r["detail"][0]
    assert "可清 6" in r["summary"], r["summary"]


def test_manual_backups_never_judged(hub_root, tmp_path):
    """③ 人工命名备份（bak-20260810 / bak-token-*）只统计，不判红、不参与上限。"""
    brain.init_db(hub_root)
    d = tmp_path / "agents"
    d.mkdir(parents=True)
    for tag in ("20260810", "token-20261001", "addmcp", "gh-token-0903"):
        (d / f"config.yaml.bak-{tag}").write_text("x", encoding="utf-8")
    r = ops._hygiene(hub_root, dirs=[("agents", d)])
    assert r["ok"] is True, r
    assert any("人工命名备份 4 个" in x for x in r["detail"]), r["detail"]


def test_prune_keeps_only_own_backups(tmp_path):
    """④ _prune_backups 清到 keep 份，且不碰人工命名文件与普通文件。"""
    d = tmp_path / "p"
    d.mkdir()
    f = d / "cfg.json"
    f.write_text("{}", encoding="utf-8")
    _mk(d, "cfg.json", 20)
    manual = d / "cfg.json.bak-20260810"
    manual.write_text("人工备份", encoding="utf-8")
    other = d / "别的文件.txt"
    other.write_text("无关", encoding="utf-8")

    core._backup(f)                                  # 触发裁剪
    left = sorted(d.glob("cfg.json.bak-agenthub-*"))
    assert len(left) == core.BACKUP_KEEP, (len(left), core.BACKUP_KEEP)
    assert manual.is_file(), "删掉了人工命名备份"
    assert other.is_file(), "删掉了非备份文件"


def test_keep_is_eight():
    """⑤ 上限本身钉死——它同时是 hygiene 的判红门槛，改它必须是有意的。"""
    assert core.BACKUP_KEEP == 8
