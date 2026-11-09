# -*- coding: utf-8 -*-
"""R1073：钩子侧最终排序改用 RRF 名次融合的 A/B。

动机：工具路径 80.5% vs 钩子 67.3%。R1066 已把「加性语义权重重一点」这条路证伪
（15/20/30 全负）；RRF 换的是**融合形状**（看名次不看分数尺度），不是同一个旋钮。

纪律（同 R1063/R1069）：
  · 只切 RECALL_RRF_K，调 prompt_recall 本体；
  · 每档一份**真库快照**（tools/_abroot.py）——否则上一档的注入计数会进下一档的排序分；
  · 先由基准档判题面泄漏并锁分母，各档同分母；
  · 报 Recall@3 / 噪声格 / 耗时，判据写死：Δrate>0 且 Δ噪声<=0 且 Δ耗时<=50ms。

用法：
    D:/python311/python.exe tools/popularity_bias_ab_r1072.py
    D:/python311/python.exe tools/popularity_bias_ab_r1072.py --out ../output/xxx.txt
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
import _abroot  # noqa: E402
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

# # 0 = off（现状）；其余为 RRF 的 k 值
CONFIGS = [0, 60, 10, 100]


def _one(cfg, root, cases, state, leak_ids=frozenset()):
    brain.RECALL_RRF_K = cfg
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
                session=f"rrf-{i}", state_path=str(state),
                update_state=False, origin="eval")
        except Exception as exc:  # noqa: BLE001 单条异常不中断量具
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
    return {"cfg": cfg, "hit": counts["hit"], "scored": scored,
            "rate": round(rate, 1), "counts": counts, "noise": noise,
            "avg_ms": round(avg_ms, 1), "per_case": per_case}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = args.root or ev._brain_root()
    cases = ev.CASES
    state = Path(tempfile.mkdtemp(prefix="rrf_ab_")) / "state.json"
    old = brain.RECALL_RRF_K

    buf = io.StringIO()
    buf.write(f"R1073 RRF 融合 A/B | 真库 {root} | 样本 {len(cases)} 条 | 档位 {CONFIGS}\n")
    buf.write(ev.env_guard_line(root) + "\n\n")

    _snap0 = _abroot.snapshot_root(root)
    try:
        probe = _one(CONFIGS[0], _snap0, cases,
                     Path(tempfile.mkdtemp(prefix="rrf_probe_")) / "state.json")
    finally:
        _abroot.drop_root(_snap0)
    leak_ids = frozenset(i for i, cls, _ in probe["per_case"] if cls == "leak")

    results = []
    for cfg in CONFIGS:
        snap = _abroot.snapshot_root(root)
        try:
            r = _one(cfg, snap, cases, state, leak_ids)
        finally:
            _abroot.drop_root(snap)
        results.append(r)
        buf.write(f"k={cfg:<4d}  "
                  f"Recall@3={r['hit']}/{r['scored']}={r['rate']:5.1f}%  "
                  f"噪声格={r['noise']:3d}  均耗时={r['avg_ms']:6.1f}ms  四类={r['counts']}\n")

    buf.write(f"\n分母锁定：剔除题面泄漏样本 {sorted(leak_ids)}，"
              f"各档分母恒为 {results[0]['scored']}。\n")
    base = results[0]
    buf.write("\n相对现状档：\n")
    for r in results[1:]:
        flips = [(i, a, b) for (i, a, _), (j, b, _) in
                 zip(base["per_case"], r["per_case"]) if i == j and a != b]
        gained = [f for f in flips if f[1] != "hit" and f[2] == "hit"]
        lost = [f for f in flips if f[1] == "hit" and f[2] != "hit"]
        buf.write(f"  {r['cfg']}  Δrate={r['rate'] - base['rate']:+5.1f}pp  "
                  f"Δ噪声={r['noise'] - base['noise']:+3d}  "
                  f"Δ耗时={r['avg_ms'] - base['avg_ms']:+6.1f}ms  "
                  f"转命中={gained}  转丢失={lost}\n")
    buf.write("\n判据（写在前）：Δrate>0 且 Δ噪声<=0 且 Δ耗时<=50ms 才考虑改默认档。\n")
    text = buf.getvalue()
    print(text)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"已写：{p}")
    brain.RECALL_RRF_K = old
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
