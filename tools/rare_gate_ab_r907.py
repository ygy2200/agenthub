# -*- coding: utf-8 -*-
"""R907：稀有词闸门「弱候选兜底」的同库 A/B 量具。

背景（R906 全量归因，32 条语料 / 51 个期望答案）：
  · rare_gate 12 条 = 最大淘汰原因；not_in_pool 6 / passed_gates 8 / hit_gate 2；
  · **32 条样本 sem_scored 全为 0** —— R901 的 replace/0.62 语义救援、R899 的语义
    重排在这条路径上一次都不触发，所以「调语义权重」不可能救回这 12 条。

本量具只切 `brain.RECALL_WEAK_PICK_MIN` 一个常量，每档都跑 `brain.prompt_recall`
本体（记忆 #511：离屏模拟器不算验收）。隔离同 R900：origin=eval + 临时 state +
update_state=False，不把真库记忆推成 30 天免疫。

判据（写在前，不许事后改）：只有「ΔRecall@3 > 0 且 Δ噪声格 <= 0 且补位精准率 > 0」
才允许把默认值从 0 改成胜出档；否则方向证伪，默认维持 off 并关账。

用法：
    D:/python311/python.exe tools/rare_gate_ab_r907.py
    D:/python311/python.exe tools/rare_gate_ab_r907.py --configs "[0,3,4,5]" --out <path>
"""
import argparse
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

DEFAULT_CONFIGS = [0, 2, 3, 4, 5, 6]


def _one(min_hits: int, root: str, cases: list, state: Path,
         leak_ids=frozenset()) -> dict:
    counts = {"hit": 0, "gap": 0, "miss": 0, "leak": 0}
    noise = 0
    picked_n = 0
    picked_right = 0
    per_case = []
    brain.RECALL_WEAK_PICK_MIN = int(min_hits)
    for i, (prompt, want, _why) in enumerate(cases, 1):
        if i in leak_ids:
            continue
        accepts = ev._accepts(want)
        if not accepts:
            continue
        try:
            res = brain.prompt_recall(
                root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"ab-weak-{min_hits}-{i}", state_path=str(state),
                update_state=False, origin="eval")
        except Exception as e:  # noqa: BLE001 量具不能因单条炸掉而中断
            per_case.append((i, "ERROR", [], 0))
            continue
        cls, _span, _leak = ev._classify(prompt, accepts, res, root)
        counts[cls] += 1
        mem_top = list(res.get("memory_ids") or [])[:3]
        rec_top = list(res.get("record_ids") or [])[:3]
        got = [(k, int(v)) for k, ids in (("mem", mem_top), ("rec", rec_top))
               for v in ids]
        noise += sum(1 for pair in got if pair not in accepts)
        if res.get("weak_picked"):
            picked_n += 1
            picked_right += int(("mem", int(res.get("weak_picked_id") or 0)) in accepts)
        per_case.append((i, cls, [f"{k}#{v}" for k, v in got],
                         int(res.get("weak_picked") or 0)))
    scored = sum(counts.values()) - counts["leak"]
    rate = (counts["hit"] / scored * 100) if scored else 0.0
    return {"min_hits": int(min_hits), "hit": counts["hit"], "scored": scored,
            "rate": round(rate, 1), "counts": counts, "noise": noise,
            "picked": picked_n, "picked_right": picked_right,
            "per_case": per_case}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--configs", default="")
    args = ap.parse_args()
    root = args.root or ev._brain_root()
    cases = ev.CASES
    configs = json.loads(args.configs) if args.configs else DEFAULT_CONFIGS
    state = Path(tempfile.mkdtemp(prefix="weak_ab_")) / "state.json"
    saved = brain.RECALL_WEAK_PICK_MIN

    buf = io.StringIO()
    buf.write(f"R907 弱候选兜底 A/B | 真库 {root} | 样本 {len(cases)} 条\n")
    buf.write(f"档位（最少命中不同检索词数，0=off）：{configs}\n")
    buf.write("判据：Δrate>0 且 Δ噪声<=0 且 补位精准率>0 才允许改默认档\n\n")
    try:
        # 与 R900 同款：先独立探测 off 档锁死题面泄漏剔除集，所有档共用同一分母。
        probe_state = Path(tempfile.mkdtemp(prefix="weak_probe_")) / "state.json"
        probe = _one(0, root, cases, probe_state)
        leak_ids = frozenset(i for i, cls, _g, _p in probe["per_case"] if cls == "leak")
        results = []
        for cfg in configs:
            r = _one(int(cfg), root, cases, state, leak_ids)
            results.append(r)
            prec = (r["picked_right"] / r["picked"] * 100) if r["picked"] else 0.0
            buf.write(f"min={r['min_hits']}  Recall@3={r['hit']}/{r['scored']}"
                      f"={r['rate']:5.1f}%  噪声格={r['noise']:3d}  "
                      f"补位={r['picked']:2d}  精准={r['picked_right']}/{r['picked']}"
                      f"({prec:.0f}%)  四类={r['counts']}\n")
        base = results[0]
        buf.write(f"\n分母锁定：剔除题面泄漏样本 {sorted(leak_ids)}，"
                  f"各档分母恒为 {base['scored']}。\n相对 off 档：\n")
        winners = []
        for r in results[1:]:
            d_rate = r["rate"] - base["rate"]
            d_noise = r["noise"] - base["noise"]
            flips = [(i, a, b) for (i, a, _g, _p), (j, b, _g2, _p2) in
                     zip(base["per_case"], r["per_case"]) if i == j and a != b]
            gained = [f for f in flips if f[1] != "hit" and f[2] == "hit"]
            lost = [f for f in flips if f[1] == "hit" and f[2] != "hit"]
            prec = (r["picked_right"] / r["picked"]) if r["picked"] else 0.0
            ok = d_rate > 0 and d_noise <= 0 and prec > 0
            buf.write(f"min={r['min_hits']}  Δrate={d_rate:+5.1f}pp  "
                      f"Δ噪声={d_noise:+d}  转命中={[f[0] for f in gained]}  "
                      f"转丢失={[f[0] for f in lost]}  补位精准={prec:.0%}  "
                      f"→ {'达标' if ok else '不达标'}\n")
            if ok:
                winners.append(r["min_hits"])
        buf.write("\n结论：")
        if winners:
            buf.write(f"达标档 {winners}，可考虑把默认改成其中收益最高的一档"
                      "（仍需人工复核转命中样本是不是真答案）。\n")
        else:
            buf.write("无一档达标 → 方向证伪，默认维持 RECALL_WEAK_PICK_MIN=0，"
                      "只保留开关与 A/B 量具供日后复盘，不动生产行为。\n")
        text = buf.getvalue()
        print(text, end="")
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        return 0
    finally:
        brain.RECALL_WEAK_PICK_MIN = saved


if __name__ == "__main__":
    raise SystemExit(main())
