# -*- coding: utf-8 -*-
"""R918：全库稀有度救援的同库 A/B（默认 off，只在 gap 场景补 1 条）。

R917 把「全盘接管 rare 判定」证伪了（corpus_rare 6/14=42.9%<50%），但 6 条真
误杀里有 #24「组管」1.32%、#26「帧生」1.41%、#10「简报」1.02%，恰好覆盖三条
gap 中的两条。本量具只切 `brain.RECALL_CORPUS_RESCUE` 与阈值，验这个窄口子。

判据（写在前）：Δrate>0 且 Δ噪声<=0 才允许改默认档；否则证伪并维持 off。
隔离同 R900：origin=eval + 临时 state + update_state=False；报表头挂 R910 环境守卫。

用法：
    AGENTHUB_SEMANTIC=0 D:/python311/python.exe tools/corpus_rescue_ab_r918.py
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

DEFAULT_CONFIGS = [("off", 0.02), ("gap", 0.01), ("gap", 0.02),
                   ("gap", 0.03), ("gap", 0.05)]


def _one(mode: str, ratio: float, root: str, cases: list, state: Path,
         leak_ids=frozenset()) -> dict:
    counts = {"hit": 0, "gap": 0, "miss": 0, "leak": 0}
    noise, rescued, rescued_right = 0, 0, 0
    per_case = []
    brain.RECALL_CORPUS_RESCUE = mode
    brain.RECALL_CORPUS_RARE_RATIO = float(ratio)
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
                session=f"ab-corpus-{mode}-{ratio}-{i}", state_path=str(state),
                update_state=False, origin="eval")
        except Exception as e:  # noqa: BLE001
            per_case.append((i, "ERROR", []))
            continue
        cls, _span, _leak = ev._classify(prompt, accepts, res, root)
        counts[cls] += 1
        got = [(k, int(v)) for k, ids in
               (("mem", list(res.get("memory_ids") or [])[:3]),
                ("rec", list(res.get("record_ids") or [])[:3])) for v in ids]
        noise += sum(1 for pair in got if pair not in accepts)
        if res.get("corpus_rescued"):
            rescued += 1
            rescued_right += int(("mem", int(res.get("corpus_rescued_id") or 0))
                                 in accepts)
        per_case.append((i, cls, [f"{k}#{v}" for k, v in got]))
    elapsed = time.perf_counter() - t0
    scored = sum(counts.values()) - counts["leak"]
    rate = (counts["hit"] / scored * 100) if scored else 0.0
    return {"mode": mode, "ratio": float(ratio), "hit": counts["hit"],
            "scored": scored, "rate": round(rate, 1), "counts": counts,
            "noise": noise, "rescued": rescued, "rescued_right": rescued_right,
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
    configs = [tuple(c) for c in (json.loads(args.configs) if args.configs
                                  else DEFAULT_CONFIGS)]
    state = Path(tempfile.mkdtemp(prefix="corpus_ab_")) / "state.json"
    saved = (brain.RECALL_CORPUS_RESCUE, brain.RECALL_CORPUS_RARE_RATIO)
    buf = io.StringIO()
    buf.write(f"R918 全库稀有度救援 A/B | 真库 {root} | 样本 {len(cases)} 条\n")
    buf.write(ev.env_guard_line(root) + "\n")
    buf.write(f"档位：{configs}\n判据：Δrate>0 且 Δ噪声<=0 才允许改默认档\n\n")
    try:
        probe_state = Path(tempfile.mkdtemp(prefix="corpus_probe_")) / "state.json"
        probe = _one("off", 0.02, root, cases, probe_state)
        leak_ids = frozenset(i for i, cls, _g in probe["per_case"] if cls == "leak")
        results = []
        for mode, ratio in configs:
            r = _one(mode, ratio, root, cases, state, leak_ids)
            results.append(r)
            prec = (r["rescued_right"] / r["rescued"] * 100) if r["rescued"] else 0.0
            buf.write(f"{mode:<4s} ratio={ratio:.2f}  Recall@3={r['hit']}/{r['scored']}"
                      f"={r['rate']:5.1f}%  噪声={r['noise']:3d}  救援={r['rescued']:2d}"
                      f" 精准={r['rescued_right']}/{r['rescued']}({prec:.0f}%)"
                      f"  {r['ms_per_case']:6.1f}ms/条\n")
        base = results[0]
        buf.write(f"\n分母锁定：剔除 {sorted(leak_ids)}，各档分母恒为 {base['scored']}。\n")
        winners = []
        for r in results[1:]:
            d_rate, d_noise = r["rate"] - base["rate"], r["noise"] - base["noise"]
            flips = [(i, a, b) for (i, a, _g), (j, b, _g2) in
                     zip(base["per_case"], r["per_case"]) if i == j and a != b]
            gained = [f[0] for f in flips if f[1] != "hit" and f[2] == "hit"]
            lost = [f[0] for f in flips if f[1] == "hit" and f[2] != "hit"]
            ok = d_rate > 0 and d_noise <= 0
            buf.write(f"{r['mode']} {r['ratio']:.2f}  Δrate={d_rate:+5.1f}pp  "
                      f"Δ噪声={d_noise:+d}  转命中={gained}  转丢失={lost}  "
                      f"→ {'达标' if ok else '不达标'}\n")
            if ok:
                winners.append((r["mode"], r["ratio"]))
        buf.write("\n结论：" + (f"达标档 {winners}，可考虑开默认。\n" if winners else
                  "无一档达标 → 窄口子同样证伪，默认维持 off，"
                  "rare_gate 留给生产语义路开启后的 R901 救援处理。\n"))
        text = buf.getvalue()
        print(text, end="")
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        return 0
    finally:
        brain.RECALL_CORPUS_RESCUE, brain.RECALL_CORPUS_RARE_RATIO = saved


if __name__ == "__main__":
    raise SystemExit(main())
