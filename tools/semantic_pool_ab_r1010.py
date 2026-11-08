# -*- coding: utf-8 -*-
"""R1010（B035 池外子类）：语义候选补池的同库 A/B 量具。

只回答一个问题：把语义 topN 中词法池没见过的候选补进 40 条候选池，再走原有闸门，
能不能在不增加 top3 噪声的前提下提高 Recall@3。

纪律：
  · 只切 `brain.RECALL_SEM_POOL_LIMIT`，直接调 `prompt_recall` 本体，不写第二套检索器；
  · origin=eval + 临时 state，避免污染真实曝光/免疫账；
  · 先由 off 档判出题面泄漏样本并锁定分母，所有档同一分母；
  · 同时报 Recall@3、top3 非期望格数、补池条数与耗时；收益/代价缺一不可。

用法：
    D:/python311/python.exe tools/semantic_pool_ab_r1010.py
    D:/python311/python.exe tools/semantic_pool_ab_r1010.py --out ../output/_r1010_pool_ab.txt
"""
import argparse
import io
import json
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

DEFAULT_CONFIGS = [0, 3, 5, 10, 20]


def _one(cap, root, cases, state, leak_ids=frozenset()):
    brain.RECALL_SEM_POOL_LIMIT = int(cap)
    counts = {"hit": 0, "gap": 0, "miss": 0, "leak": 0}
    noise = pooled = 0
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
                session=f"pool-{cap}-{i}", state_path=str(state),
                update_state=False, origin="eval")
        except Exception as exc:  # noqa: BLE001 单条异常不该中断量具
            per_case.append((i, "ERROR", str(exc)))
            continue
        elapsed.append((time.perf_counter() - t0) * 1000)
        cls, _span, _leak = ev._classify(prompt, accepts, res, root)
        counts[cls] += 1
        mem_top = list(res.get("memory_ids") or [])[:3]
        rec_top = list(res.get("record_ids") or [])[:3]
        got = [(k, int(v)) for k, ids in (("mem", mem_top), ("rec", rec_top))
               for v in ids]
        noise += sum(1 for pair in got if pair not in accepts)
        pooled += int(res.get("sem_pool_added") or 0)
        per_case.append((i, cls, [f"{k}#{v}" for k, v in got]))
    scored = sum(counts.values()) - counts["leak"]
    rate = (counts["hit"] / scored * 100) if scored else 0.0
    avg_ms = sum(elapsed) / len(elapsed) if elapsed else 0.0
    return {"cap": int(cap), "hit": counts["hit"], "scored": scored,
            "rate": round(rate, 1), "counts": counts, "noise": noise,
            "pooled": pooled, "avg_ms": round(avg_ms, 1), "per_case": per_case}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--configs", default="")
    args = ap.parse_args()
    root = args.root or ev._brain_root()
    cases = ev.CASES
    configs = json.loads(args.configs) if args.configs else DEFAULT_CONFIGS
    state = Path(tempfile.mkdtemp(prefix="sem_pool_ab_")) / "state.json"
    old_cap = brain.RECALL_SEM_POOL_LIMIT

    buf = io.StringIO()
    buf.write(f"R1010 语义补池 A/B | 真库 {root} | 样本 {len(cases)} 条\n")
    buf.write(ev.env_guard_line(root) + "\n")
    buf.write(f"补池上限档位：{configs} | 相似度线 {brain.RECALL_SEM_POOL_SIM:.2f}\n\n")

    probe_state = Path(tempfile.mkdtemp(prefix="sem_pool_probe_")) / "state.json"
    probe = _one(0, root, cases, probe_state)
    leak_ids = frozenset(i for i, cls, _ in probe["per_case"] if cls == "leak")
    results = []
    for cap in configs:
        r = _one(cap, root, cases, state, leak_ids)
        results.append(r)
        buf.write(f"cap={r['cap']:<3d}  Recall@3={r['hit']}/{r['scored']}={r['rate']:5.1f}%  "
                  f"噪声格={r['noise']:3d}  补池={r['pooled']:3d}  均耗时={r['avg_ms']:6.1f}ms  "
                  f"四类={r['counts']}\n")

    buf.write(f"\n分母锁定：剔除题面泄漏样本 {sorted(leak_ids)}，"
              f"各档分母恒为 {results[0]['scored']}。\n")
    base = results[0]
    buf.write("\n相对 off 档：\n")
    for r in results[1:]:
        d_rate = r["rate"] - base["rate"]
        d_noise = r["noise"] - base["noise"]
        d_ms = r["avg_ms"] - base["avg_ms"]
        flips = [(i, a, b) for (i, a, _), (j, b, _) in
                 zip(base["per_case"], r["per_case"]) if i == j and a != b]
        gained = [f for f in flips if f[1] != "hit" and f[2] == "hit"]
        lost = [f for f in flips if f[1] == "hit" and f[2] != "hit"]
        buf.write(f"cap={r['cap']:<3d}  Δrate={d_rate:+5.1f}pp  Δ噪声={d_noise:+3d}  "
                  f"Δ耗时={d_ms:+6.1f}ms  转命中={gained}  转丢失={lost}\n")

    buf.write("\n判据（写在前）：Δrate>0 且 Δ噪声<=0 且 Δ耗时<=50ms 才考虑改默认档；"
              "任一不满足即保持 0=off。\n")
    text = buf.getvalue()
    print(text)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"已写：{p}")
    brain.RECALL_SEM_POOL_LIMIT = old_cap
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
