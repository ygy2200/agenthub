# -*- coding: utf-8 -*-
r"""配置备份清理（`config-prune`，R254）。

背景：R253 把 `core.BACKUP_KEEP` 降到 8，但 `_prune_backups` 只在**该文件下次被编辑**
时才触发——本机存量 66 份超限备份可能几个月不动，整洁体检报出来却没有任何立刻能做的
动作（等于把问题丢回给人）。本文件守这条缺失动作的安全边界：
  ① 默认只预演，一份都不删；
  ② `--apply` 只删到每文件 keep 份，且删的是最旧的那些（文件名时间戳序）；
  ③ 人工命名备份（bak-20260810 / bak-token-*）与普通文件**一律不碰**；
  ④ 分组按「目录 + 原文件名」，同名文件在不同目录互不干扰；
  ⑤ keep 可调，且 0 份这种极端值也不越界（只删不炸）。
"""
from pathlib import Path

import core

BACKUP_DIRS = [("a", None), ("b", None)]   # 路径在用例里填


def _mk(d: Path, name: str, n: int, start: int = 0) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"{name}.bak-agenthub-20260101-{start + i:06d}-000").write_text(f"v{i}", encoding="utf-8")


def test_dry_run_deletes_nothing(tmp_path):
    """① 预演：候选列出来，磁盘上一份不少。"""
    d = tmp_path / "a"
    _mk(d, "cfg.json", 20)
    r = core.prune_config_backups(keep=8, apply=False, dirs=[("a", d)])
    assert r["candidate_count"] == 12, r
    assert r["deleted"] == []
    assert len(list(d.glob("*.bak-agenthub-*"))) == 20


def test_apply_keeps_newest_n(tmp_path):
    """② apply：留最新 8 份，删掉的都是更旧的名字。"""
    d = tmp_path / "a"
    _mk(d, "cfg.json", 20)
    r = core.prune_config_backups(keep=8, apply=True, dirs=[("a", d)])
    assert len(r["deleted"]) == 12, r
    left = sorted(p.name for p in d.glob("cfg.json.bak-agenthub-*"))
    assert len(left) == 8
    assert left == sorted(p.name for p in d.glob("cfg.json.bak-agenthub-*"))[-8:]
    assert left[0].endswith("000012-000"), left[0]      # 最旧的保留项 = 第 13 份


def test_manual_and_foreign_files_untouched(tmp_path):
    """③ 人工命名备份 / 普通文件 / 别的后缀都不动。"""
    d = tmp_path / "a"
    _mk(d, "cfg.json", 12)
    manual = d / "cfg.json.bak-20260810"
    manual.write_text("人工", encoding="utf-8")
    token = d / "cfg.json.bak-token-20261001"
    token.write_text("凭据前备份", encoding="utf-8")
    plain = d / "cfg.json"
    plain.write_text("{}", encoding="utf-8")

    core.prune_config_backups(keep=2, apply=True, dirs=[("a", d)])
    assert manual.is_file() and token.is_file() and plain.is_file()
    assert len(list(d.glob("cfg.json.bak-agenthub-*"))) == 2


def test_grouping_is_per_file_and_per_dir(tmp_path):
    """④ 两个目录各一个同名文件：各自独立保留，不会互相挤掉。"""
    a, b = tmp_path / "a", tmp_path / "b"
    _mk(a, "AGENTS.md", 10)
    _mk(b, "AGENTS.md", 10)
    r = core.prune_config_backups(keep=3, apply=True, dirs=[("a", a), ("b", b)])
    assert len(r["deleted"]) == 14, r
    assert len(list(a.glob("AGENTS.md.bak-agenthub-*"))) == 3
    assert len(list(b.glob("AGENTS.md.bak-agenthub-*"))) == 3


def test_keep_zero_and_empty_dir(tmp_path):
    """⑤ keep=0 全清（不炸）、空目录零候选。"""
    d = tmp_path / "a"
    _mk(d, "x.yaml", 4)
    r = core.prune_config_backups(keep=0, apply=True, dirs=[("a", d)])
    assert len(r["deleted"]) == 4
    assert list(d.glob("*.bak-agenthub-*")) == []
    empty = tmp_path / "empty"
    empty.mkdir()
    r2 = core.prune_config_backups(keep=8, apply=True, dirs=[("e", empty)])
    assert r2["candidate_count"] == 0 and r2["deleted"] == []


def test_default_dirs_cover_registry(hub_root):
    """⑥ 默认清单与体检同源：core.config_backup_dirs 是唯一事实源。"""
    pairs = core.config_backup_dirs(hub_root)
    names = [n for n, _ in pairs]
    assert "claude" in names and "dsh" in names and "hermes-home" in names
    assert "brain-_hub" in names
    assert dict(pairs)["brain-_hub"] == Path(hub_root) / "_hub"
    assert "brain-_hub" not in [n for n, _ in core.config_backup_dirs()]
