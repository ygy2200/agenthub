# -*- coding: utf-8 -*-
"""R1068（B035 死区子类）：weak 语义补位档的同库 A/B 量具。

只回答一个问题：当 picked 全空、答案被稀有词线判弱时，按语义分从 weak 补位，
能不能提高 Recall@3，且不增加噪声格。

背景（R1067 逐条归因量出来的）：recall_eval 的 5 条 gap 样本全部落在
「picked 空 / scored 非空 / weak 非空」的死角——R900 replace 要求 picked 非空，
R861 零词法桥要求 scored 也空，于是三档都不接手。答案其实都在池里（命中 2–5 个词），
只是命中的全是本库高频词，被稀有词线判弱。

纪律（与 R1010 量具一致）：
  · 只切 `brain.RECALL_SEM_WEAK_MIN`，直接调 `prompt_recall` 本体；
  · origin=eval + 临时 state，不污染真实曝光/免疫账；
  · 先由 0 档判出题面泄漏样本并锁定分母，所有档同一分母；
  · 同时报 Recall@3、top3 非期望格数（噪声）、实际补入条数与耗时。

用法：
    D:/python311/python.exe tools/semantic_admit_ab_r1063.py
    D:/python311/python.exe tools/semantic_admit_ab_r1063.py --out .../输出.txt
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
import _abroot  # noqa: E402
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

DEFAULT_CONFIGS = [(0.75, 0, 0.60), (0.75, 4, 0.60), (0.75, 3, 0.60), (0.75, 3, 0.65), (0.75, 2, 0.65)]


def _one(threshold, root, cases, state, leak_ids=frozenset(), cap=1):
    brain.RECALL_SEM_WEAK_MIN, brain.RECALL_SEM_WEAK_HITS, brain.RECALL_SEM_WEAK_LOW_SIM = threshold
    brain.RECALL_SEM_WEAK_CAP = int(cap)
    counts = {"hit": 0, "gap": 0, "miss": 0, "leak": 0}
    noise = admitted = 0
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
                session=f"admit-{threshold}-{i}", state_path=str(state),
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
        admitted += int(res.get("sem_weak_picked") or 0)
        per_case.append((i, cls, [f"{k}#{v}" for k, v in got]))
    scored = sum(counts.values()) - counts["leak"]
    rate = (counts["hit"] / scored * 100) if scored else 0.0
    avg_ms = sum(elapsed) / len(elapsed) if elapsed else 0.0
    return {"min": (threshold[0], threshold[1], threshold[2]), "hit": counts["hit"], "scored": scored,
            "rate": round(rate, 1), "counts": counts, "noise": noise,
            "admitted": admitted, "avg_ms": round(avg_ms, 1), "per_case": per_case}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--configs", default="")
    ap.add_argument("--cap", type=int, default=1)
    args = ap.parse_args()
    root = args.root or ev._brain_root()
    cases = ev.CASES
    configs = json.loads(args.configs) if args.configs else DEFAULT_CONFIGS
    state = Path(tempfile.mkdtemp(prefix="sem_weak_ab_")) / "state.json"
    old_min, old_cap = (brain.RECALL_SEM_WEAK_MIN, brain.RECALL_SEM_WEAK_HITS, brain.RECALL_SEM_WEAK_LOW_SIM), brain.RECALL_SEM_WEAK_CAP

    buf = io.StringIO()
    buf.write(f"R1077 低分带+多词共现 A/B | 真库 {root} | 样本 {len(cases)} 条 | "
              f"每次最多补 {args.cap} 条\n")
    buf.write(ev.env_guard_line(root) + "\n")
    buf.write(f"准入线档位：{configs}\n\n")

    probe_state = Path(tempfile.mkdtemp(prefix="sem_admit_probe_")) / "state.json"
    _snap0 = _abroot.snapshot_root(root)
    try:
        probe = _one((0.0, 0, 0.60), _snap0, cases, probe_state)
    finally:
        _abroot.drop_root(_snap0)
    leak_ids = frozenset(i for i, cls, _ in probe["per_case"] if cls == "leak")
    results = []
    for th in configs:
        # R1069：每档一份真库快照，避免上一档的 use_count/inject_count 影响下一档
        snap = _abroot.snapshot_root(root)
        try:
            r = _one(th, snap, cases, state, leak_ids, args.cap)
        finally:
            _abroot.drop_root(snap)
        results.append(r)
        buf.write(f"cfg={r['min']}  Recall@3={r['hit']}/{r['scored']}={r['rate']:5.1f}%  "
                  f"噪声格={r['noise']:3d}  补入={r['admitted']:3d}  均耗时={r['avg_ms']:6.1f}ms  "
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
        buf.write(f"cfg={r['min']}  Δrate={d_rate:+5.1f}pp  Δ噪声={d_noise:+3d}  "
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
    (brain.RECALL_SEM_WEAK_MIN, brain.RECALL_SEM_WEAK_HITS, brain.RECALL_SEM_WEAK_LOW_SIM), brain.RECALL_SEM_WEAK_CAP = old_min, old_cap
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
