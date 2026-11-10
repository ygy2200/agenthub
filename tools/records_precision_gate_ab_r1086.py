# -*- coding: utf-8 -*-
"""R1086：记录路精度闸门 A/B —— 保住 raw 的 +18.2pp，砍掉它的 6 格噪声。

R1084/R1085 钉死的事实（同一预注册判据、标注修正后复评仍成立）：
  off（query=抽词, cap=2, margin=0.62）  Recall@2 11/22=50.0%  噪声 13
  raw（query=原句, 其余不变）             Recall@2 15/22=68.2%  噪声 19
  Δrate=+18.2pp、Δ噪声=+6、4 条转命中零转丢失 → 增益真、噪声也真，KEEP-OFF。

逐条判读 19 个非目标格：只有 2 条是同任务的合法记录，其余 17 条是跨项目
无关记录；且**绝大多数是 top2 的第 2 条**。所以该装的不是更强的召回，
而是给第 2 条装闸门：`_recall_pick_margin` 首位无条件保留、后位须够得上
首位的相对强度线（R842）。另一个更硬的形态是 cap=1（只注入首位）。

两因素必须写清（不许事后糊成一个旋钮）：
  · 因素 A：查询串形态 off/raw（R1084 常量 RECALL_REC_QUERY_MODE）
  · 因素 B：精度闸门 cap / margin（本轮新增 RECALL_REC_MARGIN，0=沿用全局）
  基准固定为**生产现状 off+cap2+m0.62**，判据仍是 Δrate>0 且 Δ噪声≤0 且 Δ耗时≤50ms。

用法：
    D:/python311/python.exe tools/records_precision_gate_ab_r1086.py --out ../output/xxx.txt
"""
import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import _ab_common as ab  # noqa: E402
import records_recall_eval as rce  # noqa: E402

# (标签, query_mode, cap, margin)
CONFIGS = [
    (("off/cap2/m.62"), ("off", 2, 0.0)),
    (("raw/cap2/m.62"), ("raw", 2, 0.0)),
    (("off/cap1"), ("off", 1, 0.0)),
    (("raw/cap1"), ("raw", 1, 0.0)),
    (("raw/cap2/m.80"), ("raw", 2, 0.80)),
    (("raw/cap2/m.90"), ("raw", 2, 0.90)),
]
_ORIG = (brain.RECALL_REC_QUERY_MODE, brain.RECALL_RECORD_CAP, brain.RECALL_REC_MARGIN)


def apply(cfg):
    mode, cap, margin = cfg
    brain.RECALL_REC_QUERY_MODE = mode
    brain.RECALL_RECORD_CAP = cap
    brain.RECALL_REC_MARGIN = margin


def restore():
    brain.RECALL_REC_QUERY_MODE, brain.RECALL_RECORD_CAP, brain.RECALL_REC_MARGIN = _ORIG


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    header = (
        "预注册判据：Δrate>0 且 Δ噪声≤0 且 Δ耗时≤50ms（相对生产现状 off/cap2/m.62）。\n"
        "两因素：A=查询串 off/raw；B=精度闸门 cap/margin。基准固定为第一行。\n"
        "注意：cap=1 会同时减少合法的第二条，须看 Δrate 是否被砍回去。\n")
    try:
        ab.run_ab(label="R1086 记录路精度闸门 A/B（Recall@2）", configs=CONFIGS,
                  apply=apply, restore=restore, root=args.root, out=args.out,
                  cases=rce.CASES_REC, judge=ab.records_judge,
                  metric="Recall@2", header=header)
    finally:
        restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
