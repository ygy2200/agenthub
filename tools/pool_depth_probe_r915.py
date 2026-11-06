# -*- coding: utf-8 -*-
"""R915：not_in_pool 到底是「limit=40 截断」还是真词汇鸿沟（只读探针）。

R906 全量归因：51 个期望答案里 6 个 `not_in_pool`（连 40 条候选池都没进），
另有 12 个 `rare_gate`。外部依据（menuagentic hybrid-search-and-reranking）给
的诊断法：先在语料里找答案串，若答案在库却没进候选集，就是 retriever 的 bug。
本探针把「没进候选集」再拆一层——把 `search_memories(limit=N)` 的 N 从 40
拉到 100/200/400：
  · `pool_cap`：扩池后答案进来了 → 纯截断问题，改一个常量就能提召回，零 LLM、
    零新依赖，是性价比最高的一档；
  · `lexical_mismatch`：扩到 400 仍不进 → 抽词/词法不匹配或被近似重复折叠，
    别名表才对得上，别拿扩池硬凑；
  · `already_in`：40 条里本来就有，问题在后面的闸门/排序，与本探针无关。

只读：`count_hits=False`（不 bump use_count）、`semantic=False`、`gate=False`，
与 prompt_recall 取池时同参；不写库、不写 state、不落 journal。

用法：
    D:/python311/python.exe tools/pool_depth_probe_r915.py
    D:/python311/python.exe tools/pool_depth_probe_r915.py --json --out <path>
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

LIMITS = (40, 100, 200, 400)


def classify(hits: dict, limits=LIMITS) -> str:
    """hits: {limit: bool}。三分类，口径写死不许事后改；limits 显式传，不改全局。"""
    if hits.get(min(limits)):
        return "already_in"
    if any(hits.get(n) for n in limits[1:]):
        return "pool_cap"
    return "lexical_mismatch"


def first_limit(hits: dict, limits=LIMITS):
    """最小能把答案捞进池的 limit；没有则 None。"""
    for n in limits:
        if hits.get(n):
            return n
    return None


def probe(root: str, cases=None, limits=LIMITS) -> dict:
    limits = tuple(limits)
    cases = cases if cases is not None else ev.CASES
    per_answer = []
    for i, (prompt, want, _why) in enumerate(cases, 1):
        accepts = [a for a in ev._accepts(want) if a[0] == "mem"]
        if not accepts:
            continue
        terms = brain.extract_prompt_terms(prompt)
        query = " ".join(t for t, _ in terms)
        pools = {}
        for n in limits:
            try:
                rows = brain.search_memories(root, query, limit=n,
                                             count_hits=False, semantic=False,
                                             gate=False)
            except Exception:  # noqa: BLE001 探针不许因单档炸掉
                rows = []
            pools[n] = {int(r.get("id") or 0) for r in rows}
        for _kind, mid in accepts:
            hits = {n: int(mid) in pools[n] for n in limits}
            per_answer.append({"case": i, "id": int(mid), "prompt": prompt,
                               "hits": hits, "stage": classify(hits, limits),
                               "first_limit": first_limit(hits, limits)})
    by_stage = defaultdict(int)
    by_limit = {str(n): 0 for n in limits}
    for a in per_answer:
        by_stage[a["stage"]] += 1
        for n, ok in a["hits"].items():
            by_limit[str(n)] += int(ok)
    return {"root": root, "cases": len(cases), "answers": len(per_answer),
            "limits": list(limits), "by_stage": dict(by_stage),
            "by_limit": by_limit, "per_answer": per_answer}


def render(rep: dict) -> str:
    lines = [f"R915 池深探针 | 真库 {rep['root']} | 样本 {rep['cases']} 条 / "
             f"期望答案 {rep['answers']} 条",
             f"档位：{rep['limits']}（prompt_recall 当前取池 limit=40）", ""]
    for n in rep["limits"]:
        lines.append(f"  limit={n:<4d} 期望答案进池 {rep['by_limit'][str(n)]:2d}"
                     f"/{rep['answers']}")
    lines.append("")
    lines.append(f"分类：already_in {rep['by_stage'].get('already_in', 0)} · "
                 f"pool_cap {rep['by_stage'].get('pool_cap', 0)}（扩池可救）· "
                 f"lexical_mismatch {rep['by_stage'].get('lexical_mismatch', 0)}（扩池无效）")
    caps = [a for a in rep["per_answer"] if a["stage"] == "pool_cap"]
    if caps:
        lines.append("扩池可救的样本（最小可用 limit）：")
        for a in caps:
            lines.append(f"  #{a['case']:02d} mem#{a['id']} → limit={a['first_limit']}"
                         f" | {a['prompt'][:38]}")
    gaps = [a for a in rep["per_answer"] if a["stage"] == "lexical_mismatch"]
    if gaps:
        lines.append("扩池无效的样本（前 12 条）：")
        for a in gaps[:12]:
            lines.append(f"  #{a['case']:02d} mem#{a['id']} | {a['prompt'][:38]}")
    lines.append("")
    lines.append("结论口径：pool_cap>0 → R916 改取池常量并同库 A/B（收益与噪声同报）；"
                 "pool_cap=0 → 扩池方向证伪，转抽词/别名表，不许拿 limit 硬凑。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--limits", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = args.root or str(ev._brain_root())
    limits = tuple(int(x) for x in args.limits.split(",")) if args.limits else LIMITS
    rep = probe(root, limits=limits)
    text = (json.dumps(rep, ensure_ascii=False, indent=1) if args.json
            else render(rep) + "\n")
    print(text, end="")
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
