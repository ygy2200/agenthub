# -*- coding: utf-8 -*-
"""R916：词法候选池深度的同库 A/B（收益 / 噪声 / 耗时三项同报）。

R915 只读探针实锤 4 个期望答案是**纯池截断**（limit 40→100 就进池），
2 个是真词汇鸿沟。扩池是零 LLM、零新依赖的召回改进，但池变大会同时改变
idf 与 rare_line（`_recall_rare_threshold(len(mems))`），所以只报 Recall@3
是自欺——噪声格与耗时必须一起量。

判据（写在前，不许事后放宽）：Δrate>0 且 Δ噪声<=0 才允许改
`brain.RECALL_POOL_LIMIT` 默认值；否则维持 40 并关账。

只切一个常量，每档都跑 `brain.prompt_recall` 本体（记忆 #511）。隔离同 R900：
origin=eval + 临时 state + update_state=False。

用法：
    D:/python311/python.exe tools/pool_limit_ab_r916.py
    AGENTHUB_SEMANTIC=0 D:/python311/python.exe tools/pool_limit_ab_r916.py --out <path>
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

DEFAULT_CONFIGS = [40, 60, 80, 100, 200]


def _one(pool_limit: int, root: str, cases: list, state: Path,
         leak_ids=frozenset()) -> dict:
    counts = {"hit": 0, "gap": 0, "miss": 0, "leak": 0}
    noise = 0
    per_case = []
    brain.RECALL_POOL_LIMIT = int(pool_limit)
    t0 = time.perf_counter()
    for i, (prompt, want, _why) in enumerate(cases, 1):
        if i in leak_ids:
            continue
        accepts = ev._accepts(want)
        if not accepts:
            continue
        try:
            res = brain.prompt_recall(
                root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"ab-pool-{pool_limit}-{i}", state_path=str(state),
                update_state=False, origin="eval")
        except Exception as e:  # noqa: BLE001 量具不能因单条炸掉而中断
            per_case.append((i, "ERROR", []))
            continue
        cls, _span, _leak = ev._classify(prompt, accepts, res, root)
        counts[cls] += 1
        got = [(k, int(v)) for k, ids in
               (("mem", list(res.get("memory_ids") or [])[:3]),
                ("rec", list(res.get("record_ids") or [])[:3])) for v in ids]
        noise += sum(1 for pair in got if pair not in accepts)
        per_case.append((i, cls, [f"{k}#{v}" for k, v in got]))
    elapsed = time.perf_counter() - t0
    scored = sum(counts.values()) - counts["leak"]
    rate = (counts["hit"] / scored * 100) if scored else 0.0
    return {"pool_limit": int(pool_limit), "hit": counts["hit"], "scored": scored,
            "rate": round(rate, 1), "counts": counts, "noise": noise,
            "ms_per_case": round(elapsed * 1000 / max(1, len(per_case)), 1),
            "per_case": per_case}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--configs", default="")
    args = ap.parse_args()
    root = args.root or str(ev._brain_root())
    cases = ev.CASES
    configs = json.loads(args.configs) if args.configs else DEFAULT_CONFIGS
    state = Path(tempfile.mkdtemp(prefix="pool_ab_")) / "state.json"
    saved = brain.RECALL_POOL_LIMIT
    buf = io.StringIO()
    buf.write(f"R916 词法池深度 A/B | 真库 {root} | 样本 {len(cases)} 条\n")
    buf.write(ev.env_guard_line(root) + "\n")
    buf.write(f"档位：{configs}（当前默认 {saved}）\n")
    buf.write("判据：Δrate>0 且 Δ噪声<=0 才允许改默认档；耗时同报\n\n")
    try:
        probe_state = Path(tempfile.mkdtemp(prefix="pool_probe_")) / "state.json"
        probe = _one(saved, root, cases, probe_state)
        leak_ids = frozenset(i for i, cls, _g in probe["per_case"] if cls == "leak")
        results = []
        for n in configs:
            r = _one(int(n), root, cases, state, leak_ids)
            results.append(r)
            buf.write(f"limit={r['pool_limit']:<4d} Recall@3={r['hit']}/{r['scored']}"
                      f"={r['rate']:5.1f}%  噪声格={r['noise']:3d}  "
                      f"耗时={r['ms_per_case']:6.1f}ms/条  四类={r['counts']}\n")
        base = next((r for r in results if r["pool_limit"] == saved), results[0])
        buf.write(f"\n分母锁定：剔除题面泄漏样本 {sorted(leak_ids)}，"
                  f"各档分母恒为 {base['scored']}。\n相对 limit={base['pool_limit']}：\n")
        winners = []
        for r in results:
            if r["pool_limit"] == base["pool_limit"]:
                continue
            d_rate = r["rate"] - base["rate"]
            d_noise = r["noise"] - base["noise"]
            flips = [(i, a, b) for (i, a, _g), (j, b, _g2) in
                     zip(base["per_case"], r["per_case"]) if i == j and a != b]
            gained = [f[0] for f in flips if f[1] != "hit" and f[2] == "hit"]
            lost = [f[0] for f in flips if f[1] == "hit" and f[2] != "hit"]
            ok = d_rate > 0 and d_noise <= 0
            buf.write(f"limit={r['pool_limit']:<4d} Δrate={d_rate:+5.1f}pp  "
                      f"Δ噪声={d_noise:+d}  转命中={gained}  转丢失={lost}  "
                      f"Δ耗时={r['ms_per_case'] - base['ms_per_case']:+6.1f}ms  "
                      f"→ {'达标' if ok else '不达标'}\n")
            if ok:
                winners.append((r["pool_limit"], r))
        buf.write("\n结论：")
        if winners:
            best = min(winners, key=lambda x: x[1]["ms_per_case"])
            buf.write(f"达标档 {[w[0] for w in winners]}，其中耗时最低的是 "
                      f"limit={best[0]}（{best[1]['ms_per_case']}ms/条）→ 可改默认档。\n")
        else:
            buf.write("无一档达标 → 扩池方向证伪，默认维持 RECALL_POOL_LIMIT=40，"
                      "4 条 pool_cap 转为抽词/别名表方向处理。\n")
        text = buf.getvalue()
        print(text, end="")
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        return 0
    finally:
        brain.RECALL_POOL_LIMIT = saved


if __name__ == "__main__":
    raise SystemExit(main())
