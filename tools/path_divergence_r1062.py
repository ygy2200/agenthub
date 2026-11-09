# -*- coding: utf-8 -*-
"""R1062：两条取候选路径的分歧面量具（只读）。

为什么要有这个文件：同一个问题在本系统里有两条召回入口，它们的开关与查询串都不同——
  ① 钩子路径 `brain.prompt_recall`：query = 抽词后的拼接串（"term1 term2 …"），
     调 `search_memories(semantic=False, gate=False)`，之后用自己的 idf/hit/rare/
     immune/cap 重排；
  ② 工具路径 `hub_search_memories` → `brain.search_memories`：query = 用户原句，
     默认 `semantic=True, gate=True`，走 RRF 融合 + 覆盖度闸门。
两条路只要候选池不同，就会出现「agent 用工具搜得到的结论，自动回忆推不出来」，
或者反过来「自动回忆推的条目，agent 自己搜反而搜不到」。用户感知到的就是
「大脑时灵时不灵」。此前没有任何量具量过这个分歧面。

本量具把差异拆成两个可控变量，四个变体各跑一遍同一批真实提问：
  hook      : search_memories(terms, limit=40, semantic=False, gate=False)
  tool      : search_memories(raw,   limit=20, semantic=True,  gate=True)
  tool_terms: search_memories(terms, limit=20, semantic=True,  gate=True)   ← 只换查询串
  hook_raw  : search_memories(raw,   limit=40, semantic=False, gate=False)  ← 只换查询串
  hook_sem  : search_memories(terms, limit=40, semantic=True,  gate=False)  ← 只开语义路
  hook_sem_raw: search_memories(raw, limit=40, semantic=True,  gate=False)  ← 语义+原句
  （hook vs hook_raw = 抽词的影响；tool vs tool_terms = 抽词的影响；
    hook_sem vs hook = 语义路的净增益；tool_terms vs hook_sem = 闸门的净代价）

口径：只读。全部调用 `count_hits=False`，不污染 use_count / last_hit；不写 journal、
不改 state。命中判定沿用 recall_eval 的「可接受集合」：任一标注 id 进 top3 即命中。

用法： python tools/path_divergence_r1062.py [-v] [--root PATH]
退出码：0（诊断量具不设门禁，避免把「分歧大」当成失败）
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  控制台 UTF-8 守卫
import brain  # noqa: E402
import recall_eval  # noqa: E402  复用 CASES / _accepts / 取根

HOOK_POOL = brain.RECALL_POOL_LIMIT          # 40
TOOL_POOL = 20                               # hub_search_memories 默认 limit


def _brain_root() -> str:
    return recall_eval._brain_root()


def _ids(rows) -> list:
    return [int(r.get("id") or 0) for r in rows]


def _hit(ids, accepts, k: int = 3) -> bool:
    want = {v for _kind, v in accepts}
    return any(i in want for i in ids[:k])


def _pool_has(ids, accepts) -> bool:
    want = {v for _kind, v in accepts}
    return any(i in want for i in ids)


def _jaccard(a, b, k: int = 5) -> float:
    sa, sb = set(a[:k]), set(b[:k])
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / max(1, len(sa | sb))


def run(root: str, verbose: bool = False) -> int:
    cases = recall_eval.CASES
    agg = {name: {"hit3": 0, "pool": 0, "n": 0, "gap": 0}
           for name in ("hook", "tool", "tool_terms", "hook_raw",
                        "hook_sem", "hook_sem_raw")}
    lat = {name: [] for name in agg}
    jac = []
    rows = []
    print(f"根目录：{root}")
    print(f"样本：{len(cases)} 条 · hook 池 {HOOK_POOL} · tool 池 {TOOL_POOL} · "
          f"RRF={brain._RRF_ENABLED} · 语义开关={brain._SEM_ENABLED}")
    for i, (prompt, want, why) in enumerate(cases, 1):
        accepts = recall_eval._accepts(want)
        if not accepts or all(v <= 0 for _k, v in accepts):
            continue
        terms = brain.extract_prompt_terms(prompt)
        q_terms = " ".join(t for t, _ in terms)
        if not q_terms:
            print(f"[{i:02d}] SKIP(抽不出词) {prompt!r}")
            continue
        variants = {}

        def _timed(name, q, limit, sem, gate):
            t0 = time.perf_counter()
            got = _ids(brain.search_memories(
                root, q, limit=limit, count_hits=False,
                semantic=sem, gate=gate))
            lat[name].append((time.perf_counter() - t0) * 1000.0)
            return got

        try:
            variants["hook"] = _timed("hook", q_terms, HOOK_POOL, False, False)
            variants["hook_raw"] = _timed("hook_raw", prompt, HOOK_POOL, False, False)
            variants["tool"] = _timed("tool", prompt, TOOL_POOL, True, True)
            variants["tool_terms"] = _timed("tool_terms", q_terms, TOOL_POOL, True, True)
            variants["hook_sem"] = _timed("hook_sem", q_terms, HOOK_POOL, True, False)
            variants["hook_sem_raw"] = _timed("hook_sem_raw", prompt, HOOK_POOL, True, False)
        except Exception as e:  # noqa: BLE001 量具不因单条炸掉中断
            print(f"[{i:02d}] ERROR {prompt!r}: {e}")
            continue
        marks = {}
        for name, ids in variants.items():
            cell = agg[name]
            cell["n"] += 1
            h3 = _hit(ids, accepts)
            hp = _pool_has(ids, accepts)
            cell["hit3"] += int(h3)
            cell["pool"] += int(hp)
            cell["gap"] += int(not ids)
            marks[name] = ("✓" if h3 else ("∅" if not ids else "✗"))
        j = _jaccard(variants["hook"], variants["tool"])
        jac.append(j)
        rows.append((i, prompt, accepts, marks, j))
        if verbose:
            want_s = "|".join(f"{k}#{v}" for k, v in accepts)
            line = " ".join(f"{k}={marks[k]}" for k in
                            ("hook", "tool", "tool_terms", "hook_raw",
                             "hook_sem", "hook_sem_raw"))
            print(f"[{i:02d}] {line} J@5={j:.2f} want={want_s} terms={q_terms}")
            print(f"      hook={variants['hook'][:5]}")
            print(f"      tool={variants['tool'][:5]}")
    print("\n== 汇总（hit@3 / 池内覆盖 / 零候选 / 单次耗时中位） ==")
    for name in ("hook", "tool", "tool_terms", "hook_raw", "hook_sem", "hook_sem_raw"):
        c = agg[name]
        n = max(1, c["n"])
        ls = sorted(lat[name]) or [0.0]
        med = ls[len(ls) // 2]
        print(f"  {name:12s} hit@3 {c['hit3']:2d}/{c['n']:2d} "
              f"({100.0 * c['hit3'] / n:5.1f}%) · 池覆盖 {c['pool']:2d}/{c['n']:2d} "
              f"· 零候选 {c['gap']} · 中位 {med:6.1f}ms")
    if jac:
        print(f"\n  hook vs tool 的 top5 Jaccard：均值 {sum(jac) / len(jac):.3f} · "
              f"完全重合 {sum(1 for x in jac if x >= 0.999)}/{len(jac)} 条")
    # 分歧明细：只列 hook 与 tool 的命中结论不一致的样本
    diff = [r for r in rows if r[3]["hook"] != r[3]["tool"]]
    print(f"\n== 结论不一致的样本（hook≠tool）：{len(diff)}/{len(rows)} ==")
    for i, prompt, accepts, marks, j in diff:
        print(f"  [{i:02d}] hook={marks['hook']} tool={marks['tool']} "
              f"J@5={j:.2f} {prompt!r}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--root", default="")
    args = ap.parse_args()
    root = args.root or _brain_root()
    if not root or not os.path.isdir(root):
        print(f"根目录不可用：{root!r}")
        return 2
    return run(root, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
