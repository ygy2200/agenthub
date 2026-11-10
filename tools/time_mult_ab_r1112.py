#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R1112 · 时间窗「乘法加权」同库 A/B（97 条 = 81 基础 + 16 错位配对 + 8 对齐）。

为什么换乘法：R1107 实测加性软加权三档与 off 逐位相同（`wid*2.2` 把 ±0.5~2.0
吞掉）。乘法按比例放大，跨越名次只需「分差 < 倍数」，不依赖分数绝对量级。

档位：on(生产 MULT=0 硬过滤) / mult0.2 / mult0.5 / mult1.0 / off(TW=0 参照)。
判据（R1092，未改）：Δrate>0 且 Δ噪声≤0 且 Δp50≤50ms 且 bootstrap 下界>0。

双子集必须同时看（R1111 实锤两者方向相反）：
  * 错位子集（时间词贴老记忆）：on 4/16、off 12/16 → 硬过滤在此必杀。
  * 对齐子集（时间词=答案日期）：on 8/8 噪声 3、off 6/8 噪声 11 → 硬过滤在此有收益。
乘法档若能在两个子集上都不输，才算真正解开 B132 的两难。
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import _abroot  # noqa: E402,F401
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402
import _ab_common as ab  # noqa: E402

_ORIG = (brain.RECALL_TIME_WINDOW, brain.RECALL_TIME_MULT)
CONFIGS = [
    ("on(生产)", (1, 0.0)),
    ("mult0.2", (1, 0.2)),
    ("mult0.5", (1, 0.5)),
    ("mult1.0", (1, 1.0)),
    ("off(参照)", (0, 0.0)),
]
PAIR_IDS = tuple(i for i, (_p, _w, why) in enumerate(ev.CASES, 1)
                 if str(why).startswith("时间词配对"))
ALIGN_IDS = tuple(i for i, (_p, _w, why) in enumerate(ev.CASES, 1)
                  if str(why).startswith("时间词对齐"))


def apply(v):
    brain.RECALL_TIME_WINDOW, brain.RECALL_TIME_MULT = v
    return f"TW={v[0]} MULT={v[1]}"


def restore():
    brain.RECALL_TIME_WINDOW, brain.RECALL_TIME_MULT = _ORIG


def _noise(res, ids):
    acc = {i: set(ev._accepts(ev.CASES[i - 1][1])) for i in ids}
    return sum(1 for i, _c, got in res["per_case"] if i in ids
               for g in got
               if (g.split("#")[0], int(g.split("#")[1])) not in acc[i])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--label", default="R1112")
    args = ap.parse_args()
    header = (f"生产默认 TW={_ORIG[0]} MULT={_ORIG[1]}；评测集 {len(ev.CASES)} 条"
              f"（错位配对 {len(PAIR_IDS)} + 对齐 {len(ALIGN_IDS)}）。\n"
              "判据（R1092）：Δrate>0 且 Δ噪声≤0 且 Δp50≤50ms 且 bootstrap 下界>0。\n")
    try:
        results = ab.run_ab(
            label=f"{args.label} 时间窗乘法加权 A/B（Recall@3, {len(ev.CASES)}条）",
            configs=CONFIGS, apply=apply, restore=restore,
            root=args.root, out=args.out, cases=ev.CASES,
            header=header, verbose=False)
    finally:
        restore()

    buf = io.StringIO()
    buf.write(f"{args.label} 双子集逐档对照\n")
    maps = {r["label"]: {i: cls for i, cls, _ in r["per_case"]} for r in results}
    for name, ids in (("错位配对", PAIR_IDS), ("对齐", ALIGN_IDS)):
        buf.write(f"\n[{name}子集 {len(ids)} 条]\n")
        for i in ids:
            row = " ".join(f"{r['label']}={maps[r['label']].get(i)}" for r in results)
            buf.write(f"  [{i:02d}] {row}\n")
        buf.write("  命中数：" + "  ".join(
            f"{r['label']}={sum(1 for i in ids if maps[r['label']].get(i) == 'hit')}"
            f"/{len(ids)}" for r in results) + "\n")
        buf.write("  噪声格：" + "  ".join(
            f"{r['label']}={_noise(r, ids)}" for r in results) + "\n")

    base = results[0]
    bmap = {i: cls for i, cls, _ in base["per_case"]}
    buf.write("\n是否真的改变了排序（与生产档逐位比较，拒收「零翻转」）\n")
    for r in results[1:]:
        cmap = {i: cls for i, cls, _ in r["per_case"]}
        diff = [i for i in bmap if bmap[i] != cmap.get(i)]
        # 逐位相同不只是四类相同，还要注入串相同
        same_ids = 0
        b_ids = {i: got for i, _c, got in base["per_case"]}
        for i, _c, got in r["per_case"]:
            if b_ids.get(i) == got:
                same_ids += 1
        buf.write(f"  {r['label']:<12} 四类变化 {len(diff):2d} 条 · "
                  f"注入串完全相同 {same_ids}/{len(bmap)} 条\n")

    ok = [r["label"] for r in results[1:-1] if r["verdict"]["pass"]]
    buf.write(f"\n满足预注册判据的乘法档：{ok or '无'}\n")
    buf.write("结论写在前：乘法档若在错位子集不丢答案、在对齐子集保住噪声优势，"
              "且 Δ噪声≤0，才算解开 B132；否则按判据保持现状，并把两难写成交拍板的"
              "量化取舍表。\n")
    text = buf.getvalue()
    print(text)
    if args.out:
        p = Path(args.out)
        p.write_text(p.read_text(encoding="utf-8") + "\n" + text, encoding="utf-8")
        print(f"已写：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
