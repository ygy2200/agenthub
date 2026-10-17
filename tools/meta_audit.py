# -*- coding: utf-8 -*-
"""R6 数据卫生诊断：元信息占比分布 + 零唤起 meta 记忆 + 重复记忆候选。

一次性诊断脚本（2026-10-17 R6）。口径与 brain.metacognition 的 meta_ratio 一致：
内容含 AgentHub / hub_ / 大脑 的记忆计为 meta（大脑关于自己的记忆）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brain  # noqa: E402

ROOT = r"D:\agent资料\zcode项目记录"
META_SQL = ("content LIKE '%AgentHub%' OR content LIKE '%hub\\_%' ESCAPE '\\' "
            "OR content LIKE '%大脑%'")


def main():
    with brain.db_conn(ROOT) as c:
        live = brain._live_sql()
        total = c.execute(f"SELECT COUNT(*) FROM memories WHERE {live}").fetchone()[0]
        meta = c.execute(f"SELECT COUNT(*) FROM memories WHERE {live} AND ({META_SQL})").fetchone()[0]
        print(f"meta: {meta}/{total} = {meta * 100 / max(1, total):.1f}%")
        print("--- by kind (meta/total) ---")
        for r in c.execute(
                f"SELECT kind, SUM(CASE WHEN {META_SQL} THEN 1 ELSE 0 END) m, COUNT(*) n "
                f"FROM memories WHERE {live} GROUP BY kind ORDER BY n DESC"):
            print(f"  {r[0]:<12} {r[1]:>3}/{r[2]:<3} ({r[1] * 100 // max(1, r[2])}%)")
        print("--- meta memories use_count 分布 ---")
        for r in c.execute(
                f"SELECT use_count, COUNT(*) n FROM memories WHERE {live} AND ({META_SQL}) "
                f"GROUP BY use_count ORDER BY use_count"):
            print(f"  use={r[0]}: {r[1]} 条")
        zero = c.execute(
            f"SELECT COUNT(*) FROM memories WHERE {live} AND use_count=0 AND ({META_SQL})"
        ).fetchone()[0]
        print(f"--- meta 且零唤起: {zero} 条 ---")
        for r in c.execute(
                f"SELECT id, kind, substr(content,1,60) FROM memories "
                f"WHERE {live} AND use_count=0 AND ({META_SQL}) ORDER BY id LIMIT 12"):
            print(f"  #{r[0]} [{r[1]}] {r[2]}")
        print("--- 非 meta 零唤起（对照） ---")
        zn = c.execute(
            f"SELECT COUNT(*) FROM memories WHERE {live} AND use_count=0 "
            f"AND NOT ({META_SQL})").fetchone()[0]
        print(f"  {zn} 条")


if __name__ == "__main__":
    main()
