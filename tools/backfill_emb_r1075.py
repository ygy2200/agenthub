# -*- coding: utf-8 -*-
"""R1075：向量覆盖率补齐（只做「缺了才补」，不重建已有向量）。

病史（真库实测 2026-11-09）：632 条 active 记忆里只有 610 条有**当前编码器**的向量——
· 22 条根本没有 embeddings 行（老数据在 R861 切 ONNX 之后再没被写过）；
· 4 条（mem#539–542）还是旧 tag `BAAI/bge-small-zh-v1.5`，而检索只认
  `bge-small-zh-v1.5|onnx-int8`，等于在语义路上**隐形**。
这条不修，任何语义侧改动（准入/补位/RRF）对这批记忆都无效——不是算法问题，是索引缺行。

做法与写入口逐字一致：同一段文本（content/tags/project 用空格拼）、同一个 `_encode`、
同一条 `_store_emb`（INSERT OR REPLACE）。只补缺，不动已有向量，所以可安全重跑。

用法：
    D:/python311/python.exe tools/backfill_emb_r1075.py --dry-run
    D:/python311/python.exe tools/backfill_emb_r1075.py
"""
import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

MISSING_SQL = """
SELECT t.id, {cols} FROM {table} t
LEFT JOIN embeddings e
  ON e.entity = '{entity}' AND e.eid = t.id AND e.model = ?
WHERE {live} AND e.eid IS NULL
ORDER BY t.id
"""


def _missing(root: str, entity: str, tag: str) -> list:
    table = "memories" if entity == "memories" else "records"
    cols = "content, tags, project" if table == "memories" else "title, content, project"
    live = "t.status='active'" if table == "memories" else "t.status='active'"
    sql = MISSING_SQL.format(cols=cols, table=table, entity=entity, live=live)
    with brain.db_conn(root) as conn:
        return conn.execute(sql, (tag,)).fetchall()


def run(root: str, dry: bool = False) -> int:
    if not brain._ensure_emb():
        print("编码器不可用（ONNX/ST 都没起来），中止")
        return 2
    tag = brain._emb_tag()
    print(f"根目录：{root}\n编码器 tag：{tag}\n模式：{'dry-run' if dry else 'write'}")

    total = 0
    for entity in ("memories", "records"):
        rows = _missing(root, entity, tag)
        print(f"\n{entity}: 缺向量 {len(rows)} 条")
        for row in rows:
            rid = int(row[0])
            if entity == "memories":
                texts = (row["content"], row["tags"], row["project"])
            else:
                texts = (row["title"], row["content"], row["project"])
            if dry:
                print(f"  would embed #{rid}")
                continue
            brain._ensure_emb_for(root, entity, rid, *texts)
            total += 1
        if not dry:
            left = len(_missing(root, entity, tag))
            print(f"  补写后仍缺：{left} 条")

    if dry:
        print("\n（dry-run 未写入）")
        return 0
    print(f"\n已补写 {total} 条向量")
    for entity in ("memories", "records"):
        with brain.db_conn(root) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM embeddings WHERE entity=? AND model=?",
                (entity, tag)).fetchone()[0]
        print(f"  {entity} 当前 tag 向量数：{n}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    root = args.root or ev._brain_root()
    return run(root, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
