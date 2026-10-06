# -*- coding: utf-8 -*-
"""检索评测（P4.1，07-检索升级计划）：对评测集逐 case 跑记忆检索，输出
MRR / Hit@1/3/5（强断言）与 weak 达成、逐查询耗时 p50/max。

评测集：tools/eval_cases.json——q/targets/weak/cat；标注规范见 07 计划 P4.1
（非 pinned 目标为主、pinned case ≤2、时间语义 weak 只要求 top5）。
全部 count_hits=False——评测不得污染唤起统计（铁律 2）。
基线数字回填 07 计划修订记录；P4.2/P4.3/P4.4 每步复测对比，负优化即回滚。

运行：python tools/eval_search.py
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brain  # noqa: E402
import core  # noqa: E402


def load_cases(path: Path) -> list:
    """加载并校验评测集：缺字段/空集/重复查询/目标 id 不在库 → 报错退出不静默。"""
    if not path.is_file():
        raise SystemExit(f"评测集缺失：{path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise SystemExit("评测集为空或不是数组")
    with brain.db_conn(core.get_root()) as conn:
        valid_ids = {r[0] for r in conn.execute(
            "SELECT id FROM memories WHERE status='active'")}
    seen, cases = set(), []
    for c in raw:
        if not isinstance(c, dict) or not c.get("q") or not c.get("targets"):
            raise SystemExit(f"缺字段：{c!r}")
        key = str(c["q"]).strip()
        if not key or key in seen:
            raise SystemExit(f"空查询或重复：{key!r}")
        seen.add(key)
        bad = [t for t in c["targets"] if t not in valid_ids]
        if bad:
            raise SystemExit(f"目标 id 不在库中：{bad}（case: {key}）")
        if not isinstance(c["targets"], list):
            raise SystemExit(f"targets 不是数组：{key}")
        cases.append(c)
    return cases


def main() -> None:
    base = Path(__file__).resolve().parent
    cases = load_cases(base / "eval_cases.json")
    root = core.get_root()
    mode = "bm25" if "--bm25" in sys.argv else "like"  # P4.2：--bm25 跑单路对比

    def do_search(q: str) -> list:
        if mode == "bm25":
            return brain._bm25_rank(root, q)  # BM25 通道本身只读不计唤起
        return brain.search_memories(root, q, count_hits=False)

    rr_sum = 0.0
    hits = {1: 0, 3: 0, 5: 0}
    n_strong = n_weak = n_weak_hit = 0
    times: list = []
    misses: list = []
    for c in cases:
        t0 = time.perf_counter()
        rows = do_search(c["q"])
        times.append(time.perf_counter() - t0)
        ids = [r["id"] for r in rows]
        ranks = [ids.index(t) + 1 for t in c["targets"] if t in ids]
        best = min(ranks) if ranks else None
        weak = bool(c.get("weak"))
        if weak:
            n_weak += 1
            if best and best <= 5:
                n_weak_hit += 1
            if not (best and best <= 5):
                misses.append((c["q"], best, c["targets"]))
            continue
        n_strong += 1
        if best:
            rr_sum += 1.0 / best
            for k in (1, 3, 5):
                if best <= k:
                    hits[k] += 1
        if not (best and best <= 3):
            misses.append((c["q"], best, c["targets"]))
    print(f"基线[{mode}]（n={len(cases)}：strong {n_strong} + weak {n_weak}）")
    print(f"MRR(strong)  = {rr_sum / n_strong:.3f}")
    print(f"Hit@1        = {hits[1]}/{n_strong} ({hits[1] * 100 // n_strong}%)")
    print(f"Hit@3        = {hits[3]}/{n_strong} ({hits[3] * 100 // n_strong}%)")
    print(f"Hit@5        = {hits[5]}/{n_strong} ({hits[5] * 100 // n_strong}%)")
    print(f"weak 达成    = {n_weak_hit}/{n_weak}（时间语义 top5）")
    print(f"耗时 p50 = {statistics.median(times) * 1000:.1f}ms  "
          f"max = {max(times) * 1000:.1f}ms")
    if misses:
        print(f"未达标 {len(misses)} 条：")
        for q, rank, tg in misses:
            print(f"  rank={rank if rank else '—'}  {q}  ->  目标{tg}")


if __name__ == "__main__":
    main()
