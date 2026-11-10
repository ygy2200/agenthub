# -*- coding: utf-8 -*-
"""R1105：时间限窗**软加权** A/B —— 想同时救回答案与不增噪声。

R1103/R1104 的两难：硬过滤把 8 条时间词配对样本压到 2/8；全关能救到 7/8
（全量 +6.3pp）却把噪声推高 +8，判据不达标。软加权（R1105）不删任何候选，
只给确认窗内的候选在 spec 上加分，窗外照常竞争 —— 理论上既救答案又不放宽候选集。

判据（写在前，相对生产 on）：Δrate>0 且 Δ噪声≤0 且 Δp50≤50ms 且
配对 bootstrap 95% 下界>0。基准固定 on(生产)，另列 off 作参照。
本轮**不改默认**（RECALL_TIME_SOFT 保持 0.0），PASS 才在下一轮谈默认档。

用法：
    D:/python311/python.exe tools/time_soft_ab_r1105.py --out ../output/xxx.txt
"""
import argparse
import io
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import _ab_common as ab  # noqa: E402
import recall_eval as ev  # noqa: E402

_ORIG = (brain.RECALL_TIME_WINDOW, brain.RECALL_TIME_SOFT)
CONFIGS = [
    ("on(生产)", (1, 0.0)),
    ("soft0.5", (1, 0.5)),
    ("soft1.0", (1, 1.0)),
    ("soft2.0", (1, 2.0)),
    ("off(参照)", (0, 0.0)),
]
PAIR_IDS = tuple(range(74, 82))


def apply(cfg):
    brain.RECALL_TIME_WINDOW, brain.RECALL_TIME_SOFT = cfg


def restore():
    brain.RECALL_TIME_WINDOW, brain.RECALL_TIME_SOFT = _ORIG


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    header = (f"生产默认 TW={_ORIG[0]} SOFT={_ORIG[1]}；评测集 {len(ev.CASES)} 条，"
              f"其中 8 条时间词配对样本（{PAIR_IDS[0]}–{PAIR_IDS[-1]}）。\n"
              "判据：Δrate>0 且 Δ噪声≤0 且 Δp50≤50ms 且 bootstrap 下界>0；"
              "本轮只出证据不改默认。\n")
    try:
        results = ab.run_ab(label="R1105 时间限窗软加权 A/B（Recall@3, 81条）",
                            configs=CONFIGS, apply=apply, restore=restore,
                            root=args.root, out=args.out, cases=ev.CASES,
                            header=header, verbose=False)
    finally:
        restore()
    on = results[0]
    buf = io.StringIO()
    buf.write("R1105 时间词配对子集专项（8 条，on → 各档）\n")
    maps = {r["label"]: {i: cls for i, cls, _ in r["per_case"]} for r in results}
    for i in PAIR_IDS:
        buf.write(f"  [{i:02d}] " + " ".join(
            f"{r['label']}={maps[r['label']].get(i)}" for r in results) + "\n")
    buf.write("  配对命中数：" + " ".join(
        f"{r['label']}={sum(1 for i in PAIR_IDS if maps[r['label']].get(i) == 'hit')}/8"
        for r in results) + "\n")
    passed = [r["label"] for r in results[1:]
              if r["verdict"]["pass"] and r["label"] != "off(参照)"]
    buf.write(f"\n满足预注册判据的软加权档：{passed or '无'}\n")
    buf.write("结论写在前：若无档通过，则 B132 的「软加权」方向证伪，"
              "默认档保持硬过滤，下一轮改查「时间词形态判别」而不是继续调权重。\n")
    text = buf.getvalue()
    print(text)
    if args.out:
        p = Path(args.out)
        p.write_text(p.read_text(encoding="utf-8") + "\n" + text, encoding="utf-8")
        print(f"已写：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
