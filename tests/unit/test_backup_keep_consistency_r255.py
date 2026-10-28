# -*- coding: utf-8 -*-
r"""库快照保留口径（R255）：`backup_brain` 与 `backup_prune` 必须是同一个数。

病史（本机实测 2026-10-28）：`backup_brain` 保留 30 份，而 `BACKUP_FAMILIES["snapshot"]`
写死 10 份给 `backup_prune` 用 → 30 份快照里永远有 20 份被判「可清理」，
`backup_health` 常年报「可清理 1374MB」。更要命的是清了也会长回来：`window.py` 每次退出
自动备份，几轮就回到 30 份。**清了又长 = 永远清不掉 = 等于不可清**，这条检查从此变成噪音。

本文件守四件事：
  ① 常量单一：`BACKUP_KEEP` 就是策略表里 snapshot 的 keep；
  ② `backup_brain` 遇到历史遗留的 30 份会剪回这个数（不是继续往 30 长）；
  ③ `backup_prune` 的默认 `keep_snapshot` 与之一致（预演量 = 实际能清量，清完归零）；
  ④ `keep=0` 不踩负零切片陷阱（`baks[:-0]` 是 `baks[:0]` = 空，会静默不删）。

注：历史 30 份用「直接造文件」模拟，不能靠连跑 backup_brain——轮转已生效，跑 29 次也只留 10 份。
"""
import shutil
from pathlib import Path

import brain
import core


def _bdir(root: Path) -> Path:
    return root / core.DIR_META / brain.BACKUP_DIR


def _seed(root: str, legacy: int = 0):
    """建库 + （可选）伪造 legacy 份历史快照。"""
    brain.init_db(root)
    brain.add_memory(root, "口径测试", "note")
    if legacy:
        b = _bdir(Path(root))
        b.mkdir(parents=True, exist_ok=True)
        src = brain.db_path(root)
        for i in range(legacy):
            shutil.copyfile(src, b / f"brain-20260101-{i:06d}.db")


def test_constant_is_single_source():
    """① 策略表里的 snapshot keep 必须就是 BACKUP_KEEP 本身。"""
    assert brain.BACKUP_FAMILIES["snapshot"][2] == brain.BACKUP_KEEP
    assert brain.BACKUP_KEEP == 10


def test_backup_brain_trims_legacy_pile(hub_root):
    """② 30 份历史遗留 + 一次备份 → 落回 BACKUP_KEEP 份。"""
    _seed(hub_root, legacy=30)
    b = _bdir(Path(hub_root))
    assert len(list(b.glob("brain-*.db"))) == 30, "前置条件：先堆到 30 份"

    assert brain.backup_brain(hub_root)
    left = sorted(p.name for p in b.glob("brain-*.db"))
    assert len(left) == brain.BACKUP_KEEP, (len(left), brain.BACKUP_KEEP)
    # 保留的必须是最新的一批：31 份剪到 10 份 = 清掉 21 份最旧的 20260101
    assert len([n for n in left if n.startswith("brain-20260101")]) == 9, left
    assert any(n.startswith("brain-2026") and not n.startswith("brain-20260101")
               for n in left), left


def test_prune_default_matches_backup_brain(hub_root):
    """③ 预演量 = 实际能清量：30 份 → 正好 30-keep 份候选。"""
    _seed(hub_root, legacy=30)
    inv = brain.backup_inventory(hub_root)
    assert inv["families"]["snapshot"]["count"] == 30
    # min_age_days=0：默认 7 天保护会让刚造的文件不被算作候选（那是另一条策略，不是口径）
    preview = brain.backup_prune(hub_root, min_age_days=0)
    snap = [c for c in preview["candidates"] if c["family"] == "snapshot"]
    assert len(snap) == 30 - brain.BACKUP_KEEP, (len(snap), brain.BACKUP_KEEP)


def test_prune_apply_lands_on_keep(hub_root):
    """③b 真清一次后再预演应归零——「清了又长回来」不再成立。"""
    _seed(hub_root, legacy=30)
    brain.backup_prune(hub_root, keep_snapshot=brain.BACKUP_KEEP, min_age_days=0, apply=True)
    assert len(list(_bdir(Path(hub_root)).glob("brain-*.db"))) == brain.BACKUP_KEEP
    again = [c for c in brain.backup_prune(hub_root, min_age_days=0)["candidates"]
             if c["family"] == "snapshot"]
    assert again == [], again


def test_keep_zero_clears_all(hub_root):
    """④ keep=0：全清且不炸（负零切片陷阱的回归）。"""
    _seed(hub_root, legacy=3)
    brain.backup_brain(hub_root, keep=0)
    assert list(_bdir(Path(hub_root)).glob("brain-*.db")) == []
