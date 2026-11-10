# -*- coding: utf-8 -*-
"""R1094：用 R1092 的配对判据 + R1093 的 65 条集，复评**已上线的** RRF 门槛 0.70。

R1074（v2.41.291）在 53 条集上以「gate 0.70 → +3.9pp / 噪声 −3」把
`RECALL_RRF_GATE_SIM=0.70` 合进了生产默认。当时判据是点估计 + Δ均值，
既没有配对检验、也没有 65 条分母。R1092 的外部口径指出这类增益可能落在噪声里。

本轮**只复评、不擅自回滚**：已上线的默认档若无显著证据，标注「待确认」并写清
「不显著 ≠ 无效」，交给后续真实样本裁决；回滚属方向级动作，需拍板。

档位（基准固定为 R1074 之前的 off）：
  off        K=0                RRF 全关
  gate0      K=60, gate=0       无条件 RRF（R1073 的 +5.8pp/噪声+17 形态）
  gate0.65   K=60, gate=0.65
  gate0.70   K=60, gate=0.70    **当前生产默认**
  gate0.80   K=60, gate=0.80

判据（R1092 升级版）：Δrate>0 且 Δ噪声≤0 且 Δp50≤50ms 且 配对 bootstrap 下界>0。

用法：
    D:/python311/python.exe tools/rrf_gate_recheck_r1094.py --out ../output/xxx.txt
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
import recall_eval as ev  # noqa: E402

CONFIGS = [
    ("off", (0, 0.0, 0.0)),
    ("gate0", (60, 0.6, 0.0)),
    ("gate0.65", (60, 0.6, 0.65)),
    ("gate0.70(生产)", (60, 0.6, 0.70)),
    ("gate0.80", (60, 0.6, 0.80)),
]
_ORIG = (brain.RECALL_RRF_K, brain.RECALL_RRF_MARGIN, brain.RECALL_RRF_GATE_SIM)


def apply(cfg):
    brain.RECALL_RRF_K, brain.RECALL_RRF_MARGIN, brain.RECALL_RRF_GATE_SIM = cfg


def restore():
    brain.RECALL_RRF_K, brain.RECALL_RRF_MARGIN, brain.RECALL_RRF_GATE_SIM = _ORIG


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    header = (
        f"当前生产默认：K={_ORIG[0]} margin={_ORIG[1]} gate={_ORIG[2]}\n"
        f"评测集 {len(ev.CASES)} 条（R1093 扩样后）；基准固定 R1074 之前的 off。\n"
        "本轮只复评不回滚：已上线默认档即使证据不足，也只标「待确认」，"
        "回滚属方向级动作需拍板。\n")
    try:
        ab.run_ab(label="R1094 RRF 门槛生产默认档复评（Recall@3, 65条）",
                  configs=CONFIGS, apply=apply, restore=restore,
                  root=args.root, out=args.out, cases=ev.CASES,
                  header=header)
    finally:
        restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
