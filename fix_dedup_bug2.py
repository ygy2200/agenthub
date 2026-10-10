# -*- coding: utf-8 -*-
"""清理重复迁移记录（2026-10-10 第二次，通用版）。

## 事故
`_migrate_records` 的去重键在 2026-10-10 被改成"不含 agent"时，只改了 `known` 集合的构造、
忘了同步改比对用的 `key` —— 两套键结构不同、永不相等 → 去重彻底失效 → **每次 init_db
全量重插**。GUI（唯一调用 init_db 的入口）在 13:18 被启动一次，于是 451 条重复入库。

## 本脚本
识别"空 agent 且存在同 (project, source, title, content) 的非空 agent 记录"的条目 = 重复品，
删除之（整行 JSON 存 journal 可还原）。默认 dry-run。

## 与前一版脚本的区别
条件不再写死 id 区间，改为**按内容配对**，避免 id 假设失效（这已是第二次同类清理）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

DB = r"D:\agent资料\zcode项目记录\_hub\brain.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # 候选：空 agent 的迁移记录
    cands = [dict(r) for r in conn.execute(
        "SELECT * FROM records WHERE status='active' AND agent='' AND source LIKE 'migrated:%'")]
    print(f"空 agent 的迁移记录：{len(cands)} 条")

    verified, orphan = [], []
    for n in cands:
        old = conn.execute(
            "SELECT id FROM records WHERE status='active' AND agent!='' AND project=? AND "
            "source=? AND title=? AND content=?",
            (n["project"], n["source"], n["title"], n["content"])).fetchone()
        (verified if old else orphan).append(n)
    print(f"  确认是重复品（有非空 agent 的同内容记录）：{len(verified)}")
    print(f"  找不到对应物（保留不动）：{len(orphan)}")
    for o in orphan[:5]:
        print(f"    ! #{o['id']} {o['project']} | {(o['title'] or '')[:40]}")

    if not args.apply:
        print("\n[dry-run] 未写库。确认后加 --apply --yes。")
        return 0
    if not args.yes:
        if input(f"确认删除 {len(verified)} 条？(yes/no) ").strip().lower() != "yes":
            print("已取消。")
            return 1

    with conn:
        for n in verified:
            conn.execute(
                "INSERT INTO journal(ts,agent,action,target,backup,note) VALUES(?,?,?,?,?,?)",
                ("2026-10-10T13:28:00", "system", "dedup_bug2",
                 f"brain:records#{n['id']}", json.dumps(n, ensure_ascii=False),
                 "去重键结构不匹配（key 与 known 不同构）导致的全量重插，删除"))
        conn.executemany("DELETE FROM records WHERE id=?", [(n["id"],) for n in verified])
    left = conn.execute("SELECT COUNT(*) FROM records WHERE status='active'").fetchone()[0]
    print(f"\n已删除 {len(verified)} 条，当前活跃记录：{left}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())