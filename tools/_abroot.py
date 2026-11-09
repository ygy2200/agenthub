# -*- coding: utf-8 -*-
"""R1069：A/B 量具的**状态隔离**（同库快照）。

病史（本轮实测）：R1063/R1068 的 A/B 在同一份真库上连续跑多档，档与档之间并不是
独立的——`prompt_recall` 每推一次都会 `_bump_access`（use_count/inject_count 上升），
而这两个值既进排序分（`min(use_count,10)*0.04`）又进曝光免疫判定
（inject_count≥5 且 adopt=0）。于是第 2 档看到的库已经和第 1 档不同：同一批次里
`min=0.72` 一次是 +2.5pp、一次是 +0.0pp，差别不是代码，是账本被前几档改了。

修法：每档开跑前用 `VACUUM INTO` 从真库做一份**一致性快照**（31MB，毫秒级），
各档在各自快照上跑，跑完删掉。这样「两档差异只能来自被切的常量」这句话才成立。
"""
import os
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path


def snapshot_root(src_root: str) -> str:
    """把 src_root 的 brain.db 快照成一个临时 root（同结构，仅 _hub/brain.db）。"""
    src_db = Path(src_root) / "_hub" / "brain.db"
    if not src_db.is_file():
        return src_root          # 没有库就原样返回（量具会用真库建库，属已知限制）
    dst = Path(tempfile.mkdtemp(prefix="abroot_" + uuid.uuid4().hex[:6] + "_"))
    (dst / "_hub").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(src_db))
    try:
        con.execute("VACUUM INTO ?", (str(dst / "_hub" / "brain.db"),))
    finally:
        con.close()
    return str(dst)


def drop_root(path: str, keep: bool = False) -> None:
    if keep or not path or not os.path.isdir(path):
        return
    if os.path.basename(path).startswith("abroot_"):
        shutil.rmtree(path, ignore_errors=True)
