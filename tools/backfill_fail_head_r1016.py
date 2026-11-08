# -*- coding: utf-8 -*-
r"""R1016 回填：把失败样本的 head 从「命令回声」换成「错误本体」。

背景：R1016 之前 `sample_failure` 直接截前 400 字，存下来的几乎全是
「失败调用：<命令>」与宿主回显的「Command: <命令>」，真正的报错正文被切掉——
所以采集面有 153 枚样本，却说不清「无签名触发到底是什么错」。

回填范围：**只动载荷完整落在 400 字以内的行**（chars<=400 = head 里存的是全文），
这类行可以无信息损失地重算出错误本体。chars>400 的行原文已被截断，
重算只会得到另一段命令回声，不碰。

用法：
    python tools/backfill_fail_head_r1016.py --root D:\agent资料\zcode项目记录 [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain  # noqa: E402
import core  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="回填 fail_events.head 为错误本体")
    ap.add_argument("--root", default="", help="大脑根目录")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--out", default="", help="旧 head 备份文件（jsonl）")
    args = ap.parse_args(argv)
    root = (args.root or (core.load_config() or {}).get("root", "")).strip()
    if not root:
        print("缺 root")
        return 2
    out = args.out or os.path.join(root, "_hub", "fail_head_backfill_r1016.jsonl")
    changed, skipped, attr_before, attr_after = 0, 0, 0, 0
    rows = []
    with brain.db_conn(root) as conn:
        conn.row_factory = brain.sqlite3.Row
        rows = list(conn.execute("SELECT id, chars, head, shape FROM fail_events"))
    plan = []
    for r in rows:
        old = r["head"] or ""
        if (r["chars"] or 0) > brain.FAIL_SAMPLE_MAX_CHARS:
            skipped += 1
            continue
        new = brain._fail_redact(brain._fail_head(old))[:brain.FAIL_SAMPLE_MAX_CHARS]
        if new == old:
            skipped += 1
            continue
        plan.append((r["id"], old, new))
    for rid, old, new in plan:
        attr_before += 1 if brain._fail_head_has_sig(old) else 0
        attr_after += 1 if brain._fail_head_has_sig(new) else 0
        changed += 1
    print(f"可回填 {changed} 枚 / 跳过 {skipped} 枚（载荷被截断或无需改）")
    print(f"回填后可归因 {attr_before} → {attr_after}")
    if args.dry_run:
        for rid, old, new in plan[:5]:
            print(f"  #{rid}\n    旧: {old[:110]}\n    新: {new[:110]}")
        return 0
    if not plan:
        return 0
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for rid, old, new in plan:
            f.write(json.dumps({"id": rid, "old_head": old, "new_head": new},
                               ensure_ascii=False) + "\n")
    with brain.db_conn(root) as conn:
        for rid, old, new in plan:
            conn.execute("UPDATE fail_events SET head=? WHERE id=?", (new, rid))
    print(f"已回填，旧 head 备份：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
