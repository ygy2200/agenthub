# -*- coding: utf-8 -*-
r"""备份裁剪对单份失败的免疫（R256）。

真缺陷（本机实测）：R253/R254 上线后跑 `release.bat` 刷新 7 个 agent 的引导，
`dsh`/`hermes` 都从 30 份收敛到 8 份，唯独 `~/.claude/CLAUDE.md` 停在 **24 份**；
同一份文件手动再调一次 `_prune_backups`，24 → 8 一次到位。原因：旧实现把 `old.unlink()`
裸放在循环里、外层 `except OSError: pass` 一裹——**有一份删不掉（被同步盘/杀软/别的进程
短暂持有）就整轮中断**，后面的全部不删，异常还被吞掉。症状是「策略看着在跑、数就是降不下来」。

本文件守四件事：
  ① 一份只读不许拖住其余（剩下的仍要删到 keep）；
  ② 删除份数如实返回（调用方/探针能看见实际发生了什么）；
  ③ 删不掉要留痕（stderr 一句，含份数与文件名）；
  ④ `backup_brain` 的库快照轮转同样免疫。
"""
import os
import stat
from pathlib import Path

import brain
import core


def _pile(d: Path, name: str, n: int) -> list:
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        p = d / f"{name}.bak-agenthub-20260101-{i:06d}-000"
        p.write_text(str(i), encoding="utf-8")
        out.append(p)
    return out


def test_readonly_file_does_not_block_prune(tmp_path):
    """① 第 3 份设只读：其余 17 份该删的必须照删，最终只多看这一份。"""
    d = tmp_path / "p"
    files = _pile(d, "cfg.json", 20)
    main = d / "cfg.json"
    main.write_text("{}", encoding="utf-8")
    locked = files[2]
    os.chmod(locked, stat.S_IREAD)
    try:
        deleted = core._prune_backups(main, keep=8)
        left = sorted(p.name for p in d.glob("cfg.json.bak-agenthub-*"))
        assert len(left) == 9, left            # 8 份合规 + 1 份删不掉的只读
        assert locked.name in left
        assert deleted == 11, deleted           # 12 份该删，11 份删成功
    finally:
        os.chmod(locked, stat.S_IWRITE)


def test_deleted_count_and_trace(tmp_path, capsys):
    """②③ 正常情况返回真实删除份数；有失败时往 stderr 留一句带文件名的痕。"""
    d = tmp_path / "p"
    files = _pile(d, "a.md", 12)
    main = d / "a.md"
    main.write_text("x", encoding="utf-8")
    assert core._prune_backups(main, keep=4) == 8
    assert capsys.readouterr().err == ""        # 全成功不吵人

    files2 = _pile(d, "b.md", 10)
    main2 = d / "b.md"
    main2.write_text("x", encoding="utf-8")
    os.chmod(files2[0], stat.S_IREAD)
    try:
        n = core._prune_backups(main2, keep=3)
        err = capsys.readouterr().err
        assert n == 6, n                        # 7 份该删，1 份删不掉
        assert "1 份备份删不掉" in err and files2[0].name in err, err
    finally:
        os.chmod(files2[0], stat.S_IWRITE)


def test_no_prune_when_within_keep(tmp_path):
    """份数没超就不动手（也不返回负数）。"""
    d = tmp_path / "p"
    _pile(d, "c.md", 5)
    main = d / "c.md"
    main.write_text("x", encoding="utf-8")
    assert core._prune_backups(main, keep=8) == 0
    assert len(list(d.glob("c.md.bak-agenthub-*"))) == 5


def test_backup_brain_survives_locked_snapshot(hub_root):
    """④ 库快照轮转：一份快照只读，其余照删（不中断）。"""
    brain.init_db(hub_root)
    brain.add_memory(hub_root, "锁测试", "note")
    bdir = Path(hub_root) / core.DIR_META / brain.BACKUP_DIR
    bdir.mkdir(parents=True, exist_ok=True)
    src = brain.db_path(hub_root)
    made = []
    for i in range(14):
        dst = bdir / f"brain-20260101-{i:06d}.db"
        dst.write_bytes(src.read_bytes())
        made.append(dst)
    locked = made[0]
    os.chmod(locked, stat.S_IREAD)
    try:
        assert brain.backup_brain(hub_root)          # 触发轮转（15 份 → keep）
        left = sorted(p.name for p in bdir.glob("brain-*.db"))
        assert len(left) == brain.BACKUP_KEEP + 1, left   # keep 份 + 删不掉的 1 份
        assert locked.name in left
    finally:
        os.chmod(locked, stat.S_IWRITE)
