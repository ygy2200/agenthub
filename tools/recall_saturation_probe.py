# -*- coding: utf-8 -*-
"""逐句回忆的饱和度量尺（R842）：用真实历史提问回放，量「每次注入几条」。

要回答的问题：选择器是在「挑最相关」，还是在「凑满配额」？
真实库 2026-11-02 实测：284 次 prompt_recall 里 258 次 hits=3（配额上限），
只有 3 次是 0——不是「总有 3 条同样相关」，而是后位候选几乎不受约束。

用法（只读，不回写任何计数）：

    python tools/recall_saturation_probe.py --limit 120
    python tools/recall_saturation_probe.py --limit 120 --margin 0     # 关闭相对阈值（对照）
    python tools/recall_saturation_probe.py --limit 120 --json out.json

回放纪律：
  · `_bump_inject` / `log_search` / `_record_adherence` 全部换成空函数——回放不是真实
    曝光，写进 inject_count 会喂坏 R830 的免疫断路器（那个账本靠真曝光算）；
  · update_state=False：不碰会话去重状态与额度文件；
  · 查询本身取自 searches 表的历史行，不改写、不合成。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brain  # noqa: E402
import core  # noqa: E402
import _console  # noqa: E402,F401  控制台 UTF-8 守卫（R179）

core.JOURNAL_SINK = brain.journal_add


def _queries(root: str, limit: int, days: int) -> list:
    with brain.db_conn(root) as conn:
        rows = conn.execute(
            "SELECT query, MAX(ts) ts FROM searches WHERE tool='prompt_recall' "
            "AND ts>=date('now',?) AND TRIM(COALESCE(query,''))<>'' "
            "GROUP BY query ORDER BY ts DESC LIMIT ?",
            (f"-{int(days)} day", int(limit))).fetchall()
    return [str(r["query"]) for r in rows]


def replay(root: str, queries: list, margin: float, cwd: str) -> list:
    """跑一遍回放，返回每次的 {q, n, ids, chars}。"""
    old_margin = brain.RECALL_REL_MARGIN
    brain.RECALL_REL_MARGIN = margin
    hits = []
    try:
        for q in queries:
            res = brain.prompt_recall(root, q, agent="probe", cwd=cwd,
                                      update_state=False, session="", origin="probe")
            hits.append({"q": q, "n": len(res["memory_ids"]) + len(res["record_ids"]),
                         "mem": res["memory_ids"], "rec": res["record_ids"],
                         "chars": res["chars"], "skipped": res.get("skipped", "")})
    finally:
        brain.RECALL_REL_MARGIN = old_margin
    return hits


def summarize(hits: list) -> dict:
    n = len(hits)
    if not n:
        return {"n": 0}
    counts = [h["n"] for h in hits]
    return {
        "n": n,
        "avg": round(sum(counts) / n, 2),
        "sat": round(sum(1 for c in counts if c >= 3) / n * 100, 1),
        "zero": round(sum(1 for c in counts if c == 0) / n * 100, 1),
        "dist": {str(k): counts.count(k) for k in sorted(set(counts))},
        "avg_chars": round(sum(h["chars"] for h in hits) / n),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="逐句回忆饱和度量尺（只读回放）")
    ap.add_argument("--root", default="", help="大脑根目录；默认读 ~/.agenthub/config.json")
    ap.add_argument("--limit", type=int, default=120, help="回放多少条历史提问")
    ap.add_argument("--days", type=int, default=30, help="取多少天内的提问")
    ap.add_argument("--margin", type=float, default=None,
                    help="相对阈值；缺省=用库里当前常量，0=关闭（对照）")
    ap.add_argument("--cwd", default="", help="回放用的工作目录（影响项目推断）")
    ap.add_argument("--json", default="", help="把明细写到这个文件")
    args = ap.parse_args(argv)

    root = (args.root or (core.load_config() or {}).get("root", "") or "").strip()
    if not root or not Path(root).is_dir():
        print(f"根目录不可用：{root or '(空)'}")
        return 2
    # 回放不得污染真实曝光账本（见模块 docstring）
    brain._bump_inject = lambda *a, **k: None          # noqa: SLF001
    brain.log_search = lambda *a, **k: None            # noqa: SLF001
    brain._record_adherence = lambda *a, **k: None     # noqa: SLF001

    qs = _queries(root, args.limit, args.days)
    if not qs:
        print("没有可回放的历史提问（searches 里没有 prompt_recall 行）")
        return 2
    cwd = args.cwd or root
    base_margin = brain.RECALL_REL_MARGIN if args.margin is None else args.margin
    after = replay(root, qs, base_margin, cwd)
    if args.margin is None:
        before = replay(root, qs, 0.0, cwd)     # 关闭相对阈值＝改前行为
    else:
        before = None
    s_after = summarize(after)
    print(f"回放 {s_after['n']} 条真实提问 · 相对阈值 margin={base_margin}")
    if before is not None:
        s_before = summarize(before)
        print(f"  改前（margin=0，纯 top3）：平均 {s_before['avg']} 条 / "
              f"顶满 {s_before['sat']}% / 零命中 {s_before['zero']}% / "
              f"平均 {s_before['avg_chars']} 字")
        print(f"  改后（margin={base_margin}）：平均 {s_after['avg']} 条 / "
              f"顶满 {s_after['sat']}% / 零命中 {s_after['zero']}% / "
              f"平均 {s_after['avg_chars']} 字")
        print(f"  分布 改前 {s_before['dist']}")
        print(f"  分布 改后 {s_after['dist']}")
        drop = sum(1 for b, a in zip(before, after) if a["n"] < b["n"])
        same = sum(1 for b, a in zip(before, after) if a["n"] == b["n"])
        print(f"  被收敛的提问：{drop} 条（{round(drop / s_after['n'] * 100, 1)}%）"
              f" · 不变的：{same} 条")
        lost0 = [a for b, a in zip(before, after) if b["n"] > 0 and a["n"] == 0]
        if lost0:
            print(f"  ⚠ 改前有、改后零注入：{len(lost0)} 条（首位应无条件保留，出现即缺陷）")
            for a in lost0[:3]:
                print(f"    · 「{a['q'][:40]}」")
        # 抽样看被砍掉的是什么
        shown = 0
        for b, a in zip(before, after):
            if a["n"] < b["n"] and shown < 5:
                cut = [i for i in (b["mem"] + b["rec"]) if i not in (a["mem"] + a["rec"])]
                print(f"    · 「{b['q'][:34]}」{b['n']}→{a['n']} 砍掉 {cut}")
                shown += 1
    else:
        print(f"  平均 {s_after['avg']} 条 / 顶满 {s_after['sat']}% / 零命中 {s_after['zero']}%")
        print(f"  分布 {s_after['dist']}")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"margin": base_margin, "after": after, "before": before},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  明细：{args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
