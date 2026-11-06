# -*- coding: utf-8 -*-
r"""R929：把存量 errors.sig 按当前尺子重签（默认 dry-run，只报数不写库）。

为什么单独要一个入口：`errors.sig` 是登记时算好存下来的，`_error_covered` 第一道走
「键逐字相等」——尺子改了（前 4 位按权重取，不再按字母取）不给旧行换键，等于把已登记的
修法整批降级成模糊匹配。判据写在 `brain.resign_error_sigs` 里，这个入口只负责取 root、
报前后账、控制写库与回滚。

用法：
  python tools/error_sig_resign_r929.py                 # dry-run：漂移/不变/判不了/样例
  python tools/error_sig_resign_r929.py --apply         # 真写（旧键留在 pre_sig 列）
  python tools/error_sig_resign_r929.py --apply --limit 3
  python tools/error_sig_resign_r929.py --undo          # 用 pre_sig 还原（回滚本轮）
  python tools/error_sig_resign_r929.py --root 副本目录  # 指到别处的库上试（不动生产）
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))
import _console  # noqa: E402,F401
import brain  # noqa: E402
import core  # noqa: E402


def _state_line(root: str) -> str:
    g = brain.resign_error_sigs(root, apply=False)
    return (f"扫 {g['scanned']} 行 → 漂移 {g['drifted']} · 已在新尺子 {g['same']}"
            f" · 判不了 {g['unreproducible']} · 补空键 {g['no_sig']}"
            f" · 抽不出签名 {g['no_terms']}")


def undo(root: str) -> int:
    brain.ensure_schema(root)
    with brain.db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, sig, pre_sig FROM errors"
            " WHERE pre_sig IS NULL OR COALESCE(pre_sig,'')<>''")]
        for r in rows:
            conn.execute("UPDATE errors SET sig=?, pre_sig='' WHERE id=?",
                         (r["pre_sig"] or "", r["id"]))
    print(f"已按 pre_sig 还原 {len(rows)} 行签名")
    for r in rows[:6]:
        print(f"  #{r['id']} 还原为 {r['pre_sig']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库（默认只报数）")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多重写多少行")
    ap.add_argument("--undo", action="store_true", help="用 pre_sig 还原本轮重签")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--root", default="", help="大脑根目录；缺省读 ~/.agenthub/config.json")
    args = ap.parse_args()

    root = args.root or (core.load_config() or {}).get("root", "")
    if not root:
        print("读不到大脑根目录")
        return 1
    brain.ensure_schema(root)
    if args.undo:
        return undo(root)

    print("重签前：" + _state_line(root))
    got = brain.resign_error_sigs(root, apply=args.apply, limit=args.limit)
    print(f"写库={'是' if got['applied'] and args.apply else '否(dry-run)'}"
          f" · 已重签 {got['resigned']} · 留证 {got['drifted'] + got['no_sig']} 行待还原")
    for s in got["samples"][:8]:
        print(f"  #{s['id']} [{s['status']}] {s['kind']}")
        print(f"       旧 {s['old']}")
        print(f"       新 {s['new']}")
    print("重签后：" + _state_line(root))
    d = brain.error_debt(root, days=7)
    print(f"欠账 7 天：问 {d.get('asked')} · 欠 {d.get('open')}"
          f" · 判不了 {d.get('unjudgeable')}（口径不该被本轮动过）")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"run": got, "debt_after": d}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("证据已写", args.json_out)
    if not args.apply:
        print("（dry-run：加 --apply 才写库；旧键一律留在 pre_sig 列，可还原）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
