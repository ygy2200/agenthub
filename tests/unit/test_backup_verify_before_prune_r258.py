# -*- coding: utf-8 -*-
r"""备份清理前的「删前校验」（R258）。

动机：本机 `backup_health` 报「可清理 1750MB」，一直没人按——不是不想清，是**不知道
清完还能不能回去**。所以真删之前先证明「留下的还原点能还原」：验最新整包（没整包验
最新快照），校验失败一律拒绝删除（宁可留着垃圾）。这道闸只在 `apply=True` 且确有候选
时才跑；预演不验（不白花 50ms），没候选也不验（没东西可删）。

守四件事：
  ① 还原点坏了 → 拒绝删除，一个文件都不动，并给出可读原因；
  ② 还原点好的 → 正常删，且返回里带校验摘要（用户能看见「凭什么敢删」）；
  ③ `verify_first=False` 可跳过（已知坏了但就是要清）；
  ④ 预演路径不触发校验（不增加日常开销）。
"""
import os
import sqlite3
import time
from pathlib import Path

import brain
import core


def _hub(root) -> Path:
    return Path(root) / core.DIR_META


def _good_snapshot(root, bdir: Path, name="brain-20261001-000000.db") -> Path:
    """造一份**真的可开**的快照（用 sqlite backup，不是复制字节），mtime 设为现在。"""
    src = brain.db_path(root)
    dst = bdir / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(str(src))
    d = sqlite3.connect(str(dst))
    with d:
        s.backup(d)
    d.close()
    s.close()
    return dst


def _old_files(d: Path, n: int, age_days: int = 400) -> None:
    """造 n 份过期的坏快照：内容是垃圾字节 + mtime 拨老，确保排在 good 之后。"""
    ts = time.time() - age_days * 86400
    for i in range(n):
        p = d / f"brain-20250101-{i:06d}.db"
        p.write_bytes(b"stale")
        os.utime(p, (ts, ts))


def test_broken_restore_point_blocks_delete(hub_root):
    """① 留下的快照是坏库 → 拒绝删除，候选文件一个不少。"""
    brain.init_db(hub_root)
    bdir = _hub(hub_root) / brain.BACKUP_DIR
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "brain-20260101-000000.db").write_bytes(b"not a sqlite file")   # 最新＝坏
    _old_files(bdir, 12)
    before = sorted(p.name for p in bdir.glob("brain-*.db"))

    r = brain.backup_prune(hub_root, keep_snapshot=2, min_age_days=0, apply=True)
    assert r["ok"] is False
    assert r.get("blocked") and "拒绝删除" in r["blocked"], r
    assert r["deleted"] == []
    assert sorted(p.name for p in bdir.glob("brain-*.db")) == before


def test_good_restore_point_allows_delete(hub_root):
    """② 快照可还原 → 正常删，且返回带校验摘要。"""
    brain.init_db(hub_root)
    bdir = _hub(hub_root) / brain.BACKUP_DIR
    bdir.mkdir(parents=True, exist_ok=True)
    good = _good_snapshot(hub_root, bdir)
    _old_files(bdir, 6)

    r = brain.backup_prune(hub_root, keep_snapshot=2, min_age_days=0, apply=True)
    assert r["ok"] is True, r
    assert r.get("verify", {}).get("ok") is True, r
    assert "quick_check" in str(r["verify"]["summary"]).lower() or r["verify"]["summary"]
    # 最新那份（good）永远在保留名单里
    assert good.is_file()


def test_skip_verify_allows_delete(hub_root):
    """③ verify_first=False：明知坏了也照删（人要的确定性优先）。"""
    brain.init_db(hub_root)
    bdir = _hub(hub_root) / brain.BACKUP_DIR
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "brain-20260101-000000.db").write_bytes(b"broken")
    _old_files(bdir, 8)
    r = brain.backup_prune(hub_root, keep_snapshot=2, min_age_days=0,
                           apply=True, verify_first=False)
    assert r["ok"] is True, r
    assert "verify" not in r
    assert len(list(bdir.glob("brain-*.db"))) == 2


def test_dry_run_never_verifies(hub_root):
    """④ 预演不花校验钱。"""
    brain.init_db(hub_root)
    bdir = _hub(hub_root) / brain.BACKUP_DIR
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "brain-20260101-000000.db").write_bytes(b"broken")
    _old_files(bdir, 8)
    r = brain.backup_prune(hub_root, keep_snapshot=2, min_age_days=0, apply=False)
    assert "verify" not in r and r["candidates"]


def test_no_candidates_no_verify(hub_root):
    """④b 没东西可删就不验（避免为一次空清理付校验成本）。"""
    brain.init_db(hub_root)
    bdir = _hub(hub_root) / brain.BACKUP_DIR
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "brain-20260101-000000.db").write_bytes(b"broken")
    r = brain.backup_prune(hub_root, keep_snapshot=5, min_age_days=0, apply=True)
    assert "verify" not in r and r["deleted"] == []
