#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R1110 · 时间窗「选择率护栏」同库 A/B（89 条，含 16 对配对样本）。

方向来源：R1109 把误杀机制量化成「窗口覆盖率」——单日相对 2.9%、ISO 单日
5.5%、最近N 8.5%、周 18.3%、本月 23%、月中日 49%、上月 76%；覆盖率高的
两个形态在 R1108 里一条都没恢复（没东西可误杀）。所以护栏按覆盖率而不是
按词面形态做黑名单。

档位：on(生产 MIN_COVER=0) / cover0.02 / cover0.05 / cover0.10 / cover0.20 /
      off(RECALL_TIME_WINDOW=0 全关，参照上限)。
判据沿用 R1092（写在前）：Δrate>0 且 Δ噪声≤0 且 Δp50≤50ms 且
配对 bootstrap 95% 下界>0 才考虑改默认档。

额外诊断（本轮新增，不改判据）：把 Δ噪声分解成
①转命中格贡献 ②仍失败格贡献 ③转丢失格贡献——
用来判断噪声上升是「多给了答案」还是「多给了垃圾」。
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

_ORIG_TW = brain.RECALL_TIME_WINDOW
_ORIG_COVER = brain.RECALL_TIME_MIN_COVER
CONFIGS = [
    ("on(生产)", (1, 0.0)),
    ("cover0.02", (1, 0.02)),
    ("cover0.05", (1, 0.05)),
    ("cover0.10", (1, 0.10)),
    ("cover0.20", (1, 0.20)),
    ("off(参照)", (0, 0.0)),
]
PAIR_IDS = tuple(i for i, (_p, _w, why) in enumerate(ev.CASES, 1)
                 if str(why).startswith("时间词配对"))
PAIR_N = len(PAIR_IDS)


def apply(v):
    brain.RECALL_TIME_WINDOW, brain.RECALL_TIME_MIN_COVER = v
    return f"TW={v[0]} MIN_COVER={v[1]}"


def restore():
    brain.RECALL_TIME_WINDOW = _ORIG_TW
    brain.RECALL_TIME_MIN_COVER = _ORIG_COVER


def noise_of(case, got):
    accepts = ev._accepts(case[1])
    return sum(1 for g in got if (lambda k, v: (k, v) not in accepts)(
        g.split("#")[0], int(g.split("#")[1])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    header = (f"生产默认 TW={_ORIG_TW} MIN_COVER={_ORIG_COVER}；评测集 {len(ev.CASES)} 条，"
              f"其中 {PAIR_N} 条时间词配对样本。\n"
              "判据（R1092，未改）：Δrate>0 且 Δ噪声≤0 且 Δp50≤50ms 且 "
              "bootstrap 下界>0。本轮只出证据，不改默认。\n")
    results = []
    try:
        results = ab.run_ab(label="R1110 时间窗选择率护栏 A/B（Recall@3, 89条）",
                            configs=CONFIGS, apply=apply, restore=restore,
                            root=args.root, out=args.out, cases=ev.CASES,
                            header=header, verbose=False)
    finally:
        restore()

    base = results[0]
    base_map = {i: cls for i, cls, _ in base["per_case"]}
    base_noise = {i: noise_of(ev.CASES[i - 1], got)
                  for i, _c, got in base["per_case"]}

    buf = io.StringIO()
    buf.write(f"R1110 配对子集专项（{PAIR_N} 条，on → 各档）\n")
    maps = {r["label"]: {i: cls for i, cls, _ in r["per_case"]} for r in results}
    for i in PAIR_IDS:
        line = " ".join(f"{r['label']}={maps[r['label']].get(i)}" for r in results)
        buf.write(f"  [{i:02d}] {line}\n")
    buf.write("  配对命中数：" + "  ".join(
        f"{r['label']}={sum(1 for i in PAIR_IDS if maps[r['label']].get(i) == 'hit')}/{PAIR_N}"
        for r in results) + "\n")

    buf.write("\nΔ噪声分解（相对生产档；正=新增噪声格）\n")
    for r in results[1:]:
        cand_map = {i: cls for i, cls, _ in r["per_case"]}
        cand_noise = {i: noise_of(ev.CASES[i - 1], got)
                      for i, _c, got in r["per_case"]}
        gain_n = l0 = still = 0
        for i in base_map:
            dn = cand_noise.get(i, 0) - base_noise.get(i, 0)
            if not dn:
                continue
            if base_map[i] != "hit" and cand_map.get(i) == "hit":
                gain_n += dn
            elif base_map[i] == "hit" and cand_map.get(i) != "hit":
                l0 += dn
            else:
                still += dn
        buf.write(f"  {r['label']:<12} 转命中格 {gain_n:+3d} · "
                  f"仍失败格 {still:+3d} · 转丢失格 {l0:+3d} · "
                  f"合计 {gain_n + still + l0:+3d}\n")

    buf.write("\n跳过硬过滤的配对样本（靠 out.time_window_skipped 判定）\n")
    probe = {}
    try:
        for tag, v in CONFIGS[1:-1]:
            apply(v)
            hit_ids = []
            import tempfile
            from pathlib import Path as _P
            state = _P(tempfile.mkdtemp(prefix="r1110_")) / "s.json"
            for i in PAIR_IDS:
                r = brain.prompt_recall(
                    ev._brain_root(), ev.CASES[i - 1][0], agent="workbuddy",
                    cwd=str(ROOT), session=f"r1110-{tag}-{i}",
                    state_path=str(state), update_state=False, origin="eval")
                if r.get("time_window_skipped"):
                    hit_ids.append(i)
            probe[tag] = hit_ids
    finally:
        restore()
    for tag, ids in probe.items():
        buf.write(f"  {tag:<12} 实际跳过 {len(ids):2d} 条：{ids}\n")

    passed = [r["label"] for r in results[1:-1] if r["verdict"]["pass"]]
    buf.write(f"\n满足预注册判据的护栏档：{passed or '无'}\n")
    buf.write("结论写在前：若无档通过（通常卡在 Δ噪声>0），则「按覆盖率先跳过过滤」"
              "也不能直接改默认——它是把取舍从「必丢答案」换成「多给候选」，"
              "要改默认必须另立判据（例如按 payload 精度而不是绝对格数），"
              "属方向级，需拍板。\n")
    text = buf.getvalue()
    print(text)
    if args.out:
        p = Path(args.out)
        p.write_text(p.read_text(encoding="utf-8") + "\n" + text, encoding="utf-8")
        print(f"已写：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
