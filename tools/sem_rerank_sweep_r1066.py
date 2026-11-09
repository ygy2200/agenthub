# -*- coding: utf-8 -*-
"""R1066（B035 重排权重子类）：语义重排权重 RECALL_SEM_RERANK_W 的同库 A/B 扫描。

只回答一个问题：把 prompt_recall 的**候选池**从「纯词法」换成「词法+语义融合」，
Recall@3 涨不涨、噪声与耗时付多少。下游重排/闸门一律不动——两档除这一个开关
外逐位相同，差异只能归因于池。

依据（R1062 池层实测，41 条）：纯词法池 hit@3 65.9% → 语义融合池 75.6%，
单次中位 59.6→63.0ms。本量具检验该增益能否穿过下游闸门。

用法：
    D:/python311/python.exe tools/pool_semantic_ab_r1065.py
    D:/python311/python.exe tools/pool_semantic_ab_r1065.py --out ../output/xxx.txt
"""
import argparse
import io
import sys
import tempfile
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

CONFIGS = [10.0, 15.0, 20.0, 30.0]


def _one(flag, root, cases, state, leak_ids=frozenset()):
    brain.RECALL_SEM_RERANK_W = float(flag)
    counts = {"hit": 0, "gap": 0, "miss": 0, "leak": 0}
    noise = 0
    elapsed = []
    per_case = []
    for i, (prompt, want, why) in enumerate(cases, 1):
        if i in leak_ids:
            continue
        accepts = ev._accepts(want)
        if not accepts:
            continue
        t0 = time.perf_counter()
        try:
            res = brain.prompt_recall(
                root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"rerank-{int(flag)}-{i}", state_path=str(state),
                update_state=False, origin="eval")
        except Exception as exc:  # noqa: BLE001 单条异常不该中断量具
            per_case.append((i, "ERROR", str(exc)))
            continue
        elapsed.append((time.perf_counter() - t0) * 1000)
        cls, _span, _leak = ev._classify(prompt, accepts, res, root)
        counts[cls] += 1
        got = [(k, int(v)) for k, ids in
               (("mem", list(res.get("memory_ids") or [])[:3]),
                ("rec", list(res.get("record_ids") or [])[:3]))
               for v in ids]
        noise += sum(1 for pair in got if pair not in accepts)
        per_case.append((i, cls, [f"{k}#{v}" for k, v in got]))
    scored = sum(counts.values()) - counts["leak"]
    rate = (counts["hit"] / scored * 100) if scored else 0.0
    avg_ms = sum(elapsed) / len(elapsed) if elapsed else 0.0
    return {"flag": float(flag), "hit": counts["hit"], "scored": scored,
            "rate": round(rate, 1), "counts": counts, "noise": noise,
            "avg_ms": round(avg_ms, 1), "per_case": per_case}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = args.root or ev._brain_root()
    cases = ev.CASES
    state = Path(tempfile.mkdtemp(prefix="pool_sem_ab_")) / "state.json"
    old = brain.RECALL_SEM_RERANK_W

    buf = io.StringIO()
    buf.write(f"R1065 池层语义融合 A/B | 真库 {root} | 样本 {len(cases)} 条\n")
    buf.write(ev.env_guard_line(root) + "\n\n")

    probe_state = Path(tempfile.mkdtemp(prefix="pool_sem_probe_")) / "state.json"
    probe = _one(CONFIGS[0], root, cases, probe_state)
    leak_ids = frozenset(i for i, cls, _ in probe["per_case"] if cls == "leak")
    results = []
    for flag in CONFIGS:
        r = _one(flag, root, cases, state, leak_ids)
        results.append(r)
        buf.write(f"w={r['flag']:<5.1f}  Recall@3={r['hit']}/{r['scored']}"
                  f"={r['rate']:5.1f}%  噪声格={r['noise']:3d}  "
                  f"均耗时={r['avg_ms']:6.1f}ms  四类={r['counts']}\n")

    buf.write(f"\n分母锁定：剔除题面泄漏样本 {sorted(leak_ids)}，"
              f"各档分母恒为 {results[0]['scored']}。\n")
    base = results[0]
    buf.write("\n相对基准档：\n")
    for on in results[1:]:
        flips = [(i, a, b) for (i, a, _), (j, b, _) in
                 zip(base["per_case"], on["per_case"]) if i == j and a != b]
        gained = [f for f in flips if f[1] != "hit" and f[2] == "hit"]
        lost = [f for f in flips if f[1] == "hit" and f[2] != "hit"]
        buf.write(f"  w={on['flag']:<5.1f} Δrate={on['rate'] - base['rate']:+5.1f}pp  "
                  f"Δ噪声={on['noise'] - base['noise']:+3d}  "
                  f"Δ耗时={on['avg_ms'] - base['avg_ms']:+6.1f}ms  "
                  f"转命中={gained}  转丢失={lost}\n")
    buf.write("\n判据（写在前）：Δrate>0 且 Δ噪声<=0 且 Δ耗时<=50ms 才考虑改默认档。\n")
    text = buf.getvalue()
    print(text)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"已写：{p}")
    brain.RECALL_SEM_RERANK_W = old
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
