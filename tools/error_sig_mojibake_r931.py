# -*- coding: utf-8 -*-
"""R931：迁移可证明来源的 mojibake 错误签名；默认 dry-run，写前备份，支持一代 undo。"""
import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))
import _console  # noqa: E402,F401
import brain  # noqa: E402
import core  # noqa: E402


def backup(root: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = Path(root) / "_hub" / "brain-backups" / f"brain-pre-mojibake-r931-{stamp}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(brain.db_path(root)))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return str(dest)


def undo(root: str) -> int:
    brain.ensure_schema(root)
    with brain.db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, sig, pre_sig_r930 FROM errors WHERE COALESCE(pre_sig_r930,'')<>''")]
        for row in rows:
            conn.execute("UPDATE errors SET sig=?, pre_sig_r930='' WHERE id=?",
                         (row["pre_sig_r930"], row["id"]))
    print(f"已按 pre_sig_r930 还原 {len(rows)} 行；R929 的 pre_sig 未触碰")
    for row in rows[:6]:
        print(f"  #{row['id']} {row['sig']} → {row['pre_sig_r930']}")
    return 0


def _line(stats: dict) -> str:
    return (f"扫 {stats['scanned']} → 可迁 {stats['changed']} · 不变 {stats['same']}"
            f" · 判不了 {stats['unreproducible']} · 空键不动 {stats['empty']}"
            f" · 无签名 {stats['no_terms']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--undo", action="store_true")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--root", default="")
    args = parser.parse_args()
    root = args.root or (core.load_config() or {}).get("root", "")
    if not root:
        print("读不到大脑根目录")
        return 1
    if args.undo:
        backup_path = backup(root)
        print("undo 前备份：", backup_path)
        return undo(root)

    plan = brain.resign_mojibake_error_sigs(root, limit=args.limit)
    print("迁移前：" + _line(plan))
    backup_path = ""
    if args.apply and plan["changed"]:
        backup_path = backup(root)
        print("写前备份：", backup_path)
        got = brain.resign_mojibake_error_sigs(root, apply=True, limit=args.limit)
    else:
        got = plan
    print(f"写库={'是' if args.apply else '否(dry-run)'} · 实写 {got['resigned']}")
    for sample in got["samples"][:8]:
        print(f"  #{sample['id']} 旧 {sample['old']}")
        print(f"       新 {sample['new']}")
    after = brain.resign_mojibake_error_sigs(root)
    print("迁移后：" + _line(after))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"plan": plan, "run": got, "after": after, "backup": backup_path},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print("证据已写", args.json_out)
    if not args.apply:
        print("dry-run：--apply 才写；空键与不可复现行不动，pre_sig_r930 独立留证")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
