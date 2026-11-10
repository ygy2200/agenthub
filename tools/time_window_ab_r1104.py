# -*- coding: utf-8 -*-
"""R1104：时间限窗硬过滤是否误杀答案 —— 用 R1103 的 8 条配对样本做 A/B。

R1103 实锤：给原本能命中的提问加上时间词（「上周那次」「2026-10-25 前后」
「最近三天…还记得吗」），8 条里只有 2 条还命中；逐条 dropped 显示候选池被
`RECALL_TIME_WINDOW` 砍到只剩 1–9 条，正确答案整批不在里面。
根因假设：限窗按**记忆的 created/updated 日期**硬过滤，而用户提问里的时间词
多数是「话题发生时间」或口语修饰，不等于「只想要这几天写的记忆」。

判据（写在前）：基准 = 生产现状 on(1)，候选 = 关掉限窗 off(0)。
若 off 相对 on 满足 Δrate>0、Δ噪声≤0、Δp50≤50ms 且配对 bootstrap 下界>0，
则判「硬过滤有害、证据充分」；否则只标证据不足。
本轮**只出证据不改默认**（改默认属下一轮，需单测 + 门禁 + 发版配套）。

用法：
    D:/python311/python.exe tools/time_window_ab_r1104.py --out ../output/xxx.txt
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

_ORIG = brain.RECALL_TIME_WINDOW
CONFIGS = [("on(生产)", 1), ("off", 0)]
PAIR_IDS = tuple(range(74, 82))   # R1103 追加的 8 条时间词配对样本


def apply(v):
    brain.RECALL_TIME_WINDOW = v


def restore():
    brain.RECALL_TIME_WINDOW = _ORIG


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    header = (f"生产默认 RECALL_TIME_WINDOW={_ORIG}；评测集 {len(ev.CASES)} 条，"
              f"其中 {len(PAIR_IDS)} 条为 R1103 时间词配对样本（编号 {PAIR_IDS[0]}–{PAIR_IDS[-1]}）。\n"
              "判据：off 相对 on 需 Δrate>0、Δ噪声≤0、Δp50≤50ms 且 bootstrap 下界>0"
              " 才判「硬过滤有害、证据充分」。本轮只出证据不改默认。\n")
    try:
        results = ab.run_ab(label="R1104 时间限窗 on/off A/B（Recall@3, 81条）",
                            configs=CONFIGS, apply=apply, restore=restore,
                            root=args.root, out=args.out, cases=ev.CASES,
                            header=header, verbose=False)
    finally:
        restore()
    on, off = results
    buf = io.StringIO()
    buf.write("R1104 时间词配对子集专项（8 条）\n")
    on_map = {i: cls for i, cls, _ in on["per_case"]}
    off_map = {i: cls for i, cls, _ in off["per_case"]}
    for i in PAIR_IDS:
        buf.write(f"  [{i:02d}] on={on_map.get(i):<5} → off={off_map.get(i):<5} "
                  f"{'转命中' if on_map.get(i) != 'hit' and off_map.get(i) == 'hit' else ''}\n")
    pair_on = sum(1 for i in PAIR_IDS if on_map.get(i) == "hit")
    pair_off = sum(1 for i in PAIR_IDS if off_map.get(i) == "hit")
    buf.write(f"  配对子集：on {pair_on}/8 → off {pair_off}/8\n")
    buf.write(f"  全量：on {on['hit']}/{on['scored']}={on['rate']}% "
              f"噪声={on['noise']} → off {off['hit']}/{off['scored']}={off['rate']}% "
              f"噪声={off['noise']}\n")
    v = off["verdict"]
    buf.write(f"  判据：Δrate={v['delta_rate']:+.1f}pp Δ噪声={v['delta_noise']:+d} "
              f"Δp50={v['delta_ms']:+.1f}ms g={v['gain_n']} l={v['loss_n']} "
              f"McNemar p={v['mcnemar_p']:.4f} CI=[{v['boot_lo']:+.4f},{v['boot_hi']:+.4f}] "
              f"→ {'证据充分' if v['pass'] else '证据不足'}\n")
    text = buf.getvalue()
    print(text)
    if args.out:
        p = Path(args.out)
        p.write_text(p.read_text(encoding="utf-8") + "\n" + text, encoding="utf-8")
        print(f"已写：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
