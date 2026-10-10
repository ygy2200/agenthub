# -*- coding: utf-8 -*-
"""清理 B1 阶段因去重键失配产生的重复记录（2026-10-10）。

## 事故经过
A 阶段把 `brain._migrate_records` 的 fallback 从 "zcode" 改为 ""（不再猜），
但**去重键含 agent 字段**——于是同一段文字在新规则下算出 agent=""、与库里既有的
agent="zcode" 不匹配 → 被判定为新记录 → 整批重复插入。
触发时机：任何 `ensure_schema`（MCP server 启动）都会跑增量迁移。
实测一次触发插入 451 条完全重复的迁移记录（id 2196-2646）。

## 已修
1. `brain._migrate_records` 去重键改用 (project, source, date, title, content[:256])，
   **不含 agent**——agent 是派生值、会随规则改进变化，不能作身份键。
   用 source（migrated:<文件>:<行号>）作稳定身份。
2. `core.parse_record` 加父段继承（### 子段跟随最近的 ## 父段 agent），
   让 agent 派生本身也更准。

## 本脚本
删除那 451 条重复记录（已逐字段验证与旧记录完全一致、零外键引用）。
默认 dry-run；--apply 写库；删除前把整行 JSON 存进 journal.backup 以便还原。
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
    news = [dict(r) for r in conn.execute(
        "SELECT * FROM records WHERE id>2195 AND agent='' AND status='active'")]
    print(f"候选重复记录：{len(news)} 条")

    # 二次校验：必须与既有记录逐字段一致，且零引用
    verified, bad = [], []
    for n in news:
        old = conn.execute(
            "SELECT id FROM records WHERE id<=2195 AND status='active' AND project=? AND date=? "
            "AND title=? AND content=? AND source=? AND agent!=''",
            (n["project"], n["date"], n["title"], n["content"], n["source"])).fetchone()
        (verified if old else bad).append(n)
    print(f"  逐字段验证通过：{len(verified)}")
    print(f"  验证不通过（保留不动）：{len(bad)}")
    for b in bad[:10]:
        print(f"    ! #{b['id']} {b['project']} | {b['title'][:40]}")

    if not args.apply:
        print("\n[dry-run] 未写库。确认无误后加 --apply --yes 执行。")
        return 0
    if not args.yes:
        if input(f"确认删除 {len(verified)} 条重复记录？(yes/no) ").strip().lower() != "yes":
            print("已取消。")
            return 1

    with conn:
        for n in verified:
            conn.execute(
                "INSERT INTO journal(ts,agent,action,target,backup,note) VALUES(?,?,?,?,?,?)",
                ("2026-10-10T13:00:00", "system", "dedup_migration",
                 f"brain:records#{n['id']}", json.dumps(n, ensure_ascii=False),
                 "B1去重键失配产生的重复迁移记录，删除（原件即同 source 的既有记录）"))
        conn.executemany("DELETE FROM records WHERE id=?",
                         [(n["id"],) for n in verified])
    print(f"\n已删除 {len(verified)} 条重复记录（整行 JSON 存于 journal.backup 可还原）。")
    left = conn.execute("SELECT count(*) FROM records WHERE status='active'").fetchone()[0]
    print(f"当前活跃记录：{left}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())