# -*- coding: utf-8 -*-
r"""R1022 回填：给「登记了但没签名键」的错误行补上 errors.sig。

背景：#33（WebFetch/抓 GitHub 原文失败）是用**旧代码的 MCP 进程**登记的——
行看起来一切正常（标题、现象、回滚都在），只有 errors.sig 是空的。
sig 是空的后果不是「报错」而是「退化」：召回只能靠模糊 LIKE 命中，
「登记一次下次秒命中」这句口号在那一行上不成立。

回填口径与写入时**同一把尺子**：`error_sig_of(detail) or error_sig_of(title)`。
算不出键的老行（本来就没有签名形状）不动——那是正常老行，不是缺口。

用法：
    python tools/backfill_error_sig_r1022.py --root D:\agent资料\zcode项目记录 [--dry-run]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import core  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="回填 errors.sig（缺签名键的行）")
    ap.add_argument("--root", default="", help="大脑根目录")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--out", default="", help="回填记录（jsonl）")
    args = ap.parse_args(argv)
    root = (args.root or (core.load_config() or {}).get("root", "")).strip()
    if not root:
        print("缺 root")
        return 2
    out = args.out or os.path.join(root, "_hub", "error_sig_backfill_r1022.jsonl")
    gap = brain.error_sig_gap(root, limit=200)
    print(f"缺签名键的行：{gap.get('gap', 0)} / sig 空行共 {gap.get('total', 0)}")
    if not gap.get("gap"):
        return 0
    if args.dry_run:
        for r in gap["rows"][:10]:
            print(f"  #{r['id']} → {r['sig']}  | {r['title'][:50]}")
        return 0
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with io.open(out, "w", encoding="utf-8") as f:
        for r in gap["rows"]:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with brain.db_conn(root) as conn:
        for r in gap["rows"]:
            conn.execute("UPDATE errors SET sig=? WHERE id=? AND COALESCE(sig,'')=''",
                         (r["sig"], r["id"]))
    print(f"已回填 {gap['gap']} 行，记录：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
