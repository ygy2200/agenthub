# -*- coding: utf-8 -*-
r"""R926：把历史采纳账里的脏指纹重签一遍（默认 dry-run，只报数不写库）。

为什么单独要一个入口：R925 修的是抽取器，而 `recall_adherence.fp` 是落库时算好的，
`mark_recall_used` 只拿存下来的串做 substring——旧账上还刻着坏尺子，回填通道再怎么跑
也判不动那批行（2026-11-06 取证：18 条记忆 25 个 token 不可匹配，judged_used 全 0）。
`brain.refingerprint_rows` 的判据写在函数里，这个入口只负责取 root、报前后账、控制写库。

用法：
  python tools/fp_resign_r926.py                 # dry-run：候选/跳过/样例
  python tools/fp_resign_r926.py --apply         # 真写（旧值留在 pre_fp 列，可回滚）
  python tools/fp_resign_r926.py --apply --limit 5
  python tools/fp_resign_r926.py --undo          # 用 pre_fp 把重签过的行还原回去
  python tools/fp_resign_r926.py --root 副本目录  # 指到别处的库上试（不动生产）
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))
import _console  # noqa: E402,F401
import brain  # noqa: E402
import core  # noqa: E402

NEVER = re.compile(r"[`'\"“”‘’（）()【】〔〕「」『』，。、；：！？…\s]")


def _dirty_ledger(root: str) -> dict:
    """当前账本里「判不死」的量：不可匹配 token 数 + 涉及行数/记忆数。

    R928：分段报 origin。装置行（eval/probe）永远不会交证据，混在总数里会把优先级估高
    一个量级——R925 那句「18 条记忆 120 个 token 受影响」实测生产侧只有 21 行。
    """
    out = {"tokens": 0, "rows": 0, "memories": set(),
           "prod_tokens": 0, "prod_rows": 0, "prod_memories": set(),
           "device_tokens": 0, "device_rows": 0, "device_memories": set()}
    with brain.db_conn(root) as conn:
        for r in conn.execute("SELECT memory_id, fp, origin FROM recall_adherence"):
            try:
                toks = json.loads(r["fp"] or "[]")
            except ValueError:
                continue
            bad = [t for t in toks if isinstance(t, str) and NEVER.search(t)]
            if not bad:
                continue
            mid = int(r["memory_id"] or 0)
            side = ("prod" if str(r["origin"] or "").strip().lower()
                    in brain.PRODUCTION_ORIGINS else "device")
            out["tokens"] += len(bad)
            out["rows"] += 1
            out["memories"].add(mid)
            out[side + "_tokens"] += len(bad)
            out[side + "_rows"] += 1
            out[side + "_memories"].add(mid)
    for k in ("memories", "prod_memories", "device_memories"):
        out[k] = len(out[k])
    return out


def _ledger_line(tag: str, d: dict, side: str = "") -> str:
    p = f"{side}_" if side else ""
    return (f"{tag}：判不死的 token {d[p + 'tokens']} 个 · 行 {d[p + 'rows']}"
            f" · 记忆 {d[p + 'memories']} 条")


def _stats_line(root: str) -> str:
    s = brain.recall_adherence_stats(root, days=30)
    return json.dumps(s, ensure_ascii=False)[:400]


def undo(root: str) -> int:
    with brain.db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, fp, pre_fp FROM recall_adherence"
            " WHERE COALESCE(pre_fp,'')<>''")]
        for r in rows:
            conn.execute("UPDATE recall_adherence SET fp=?, pre_fp='' WHERE id=?",
                         (r["pre_fp"], r["id"]))
    print(f"已按 pre_fp 还原 {len(rows)} 行")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库（默认只报数）")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多重写多少行")
    ap.add_argument("--days", type=int, default=brain.ADHERENCE_KEEP_DAYS)
    ap.add_argument("--undo", action="store_true", help="用 pre_fp 还原所有重签过的行")
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

    before = _dirty_ledger(root)
    print(_ledger_line("重签前", before))
    print("   " + _ledger_line("生产·只有这一栏交证据", before, "prod")
          + " · " + _ledger_line("装置", before, "device"))
    got = brain.refingerprint_rows(root, days=args.days, apply=args.apply, limit=args.limit)
    print(f"扫描 {got['scanned']} 行（窗口 {got['days']} 天）"
          f"→ 候选 {got['candidates']}（生产 {got['prod_candidates']}"
          f"·装置 {got['device_candidates']}）"
          f" · 已重签 {got['resigned']}（生产 {got['prod_resigned']}"
          f"·装置 {got['device_resigned']}）"
          f" · 跳过 {got['skipped']}"
          f" · 本就干净 {got['clean']} · 写库={'是' if got['applied'] else '否(dry-run)'}")
    for s in got["samples"][:8]:
        print(f"  #{s['memory_id']} [{s.get('origin')}] 旧 {s['old']}")
        print(f"       新 {s['new']}")
    after = _dirty_ledger(root)
    print(_ledger_line("重签后", after))
    print("   " + _ledger_line("生产", after, "prod") + " · " + _ledger_line("装置", after, "device"))
    print("采纳账三态：" + _stats_line(root))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"before": before, "after": after, "run": got}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("证据已写", args.json_out)
    if not args.apply:
        print("（dry-run：加 --apply 才写库；旧值一律留在 pre_fp 列，可还原）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
