# -*- coding: utf-8 -*-
"""R1096：生产默认档**消融体检** —— 逐个关掉已上线组件，看它到底有没有显著贡献。

R1094 用「增益」口径复评 RRF 0.70，发现证据不足。但「增益不显著」和
「关掉会不会变差」是两个不同的问题：前者问该不该合入，后者问该不该保留。
本轮换成**消融**口径，把当前生产里 6 个非默认开关逐个关掉，用 R1092 的
配对判据 + R1095 的 73 条集量一次。

判据（写在前，方向与增益档相反）：
  · 基准 = 当前生产全开（prod）；候选 = 关掉某一组件。
  · 令 d_i = cand_hit − prod_hit。若配对 bootstrap 95% 区间**上界 < 0**，
    说明关掉后显著变差 → 该组件有证据支持，保留。
  · 否则标「证据不足/待确认」——**不等于无效，更不等于该删**；71 条量级功效有限。
  · 本轮**不回滚、不改任何默认档**，只出体检表；删除已上线组件属方向级需拍板。

六组件：RRF 名次融合 / 语义准入 / weak 语义补位 / 语义重排权重 /
闸门救援 replace / 时间限窗。另加 off_all 看整体语义栈的净贡献。

用法：
    D:/python311/python.exe tools/production_ablation_r1096.py --out ../output/xxx.txt
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

KEYS = ("RECALL_RRF_K", "RECALL_SEM_ADMIT_MIN", "RECALL_SEM_WEAK_MIN",
        "RECALL_SEM_RERANK_W", "RECALL_SEM_RESCUE_MODE", "RECALL_TIME_WINDOW")
PROD = {k: getattr(brain, k) for k in KEYS}
OFF = {"RECALL_RRF_K": 0, "RECALL_SEM_ADMIT_MIN": 0,
       "RECALL_SEM_WEAK_MIN": 0, "RECALL_SEM_RERANK_W": 0,
       "RECALL_SEM_RESCUE_MODE": "off", "RECALL_TIME_WINDOW": 0}

CONFIGS = [
    ("prod", {}),
    ("off_rrf", {"RECALL_RRF_K": 0}),
    ("off_admit", {"RECALL_SEM_ADMIT_MIN": 0}),
    ("off_weak", {"RECALL_SEM_WEAK_MIN": 0}),
    ("off_rerank", {"RECALL_SEM_RERANK_W": 0}),
    ("off_rescue", {"RECALL_SEM_RESCUE_MODE": "off"}),
    ("off_time", {"RECALL_TIME_WINDOW": 0}),
    ("off_all", dict(OFF)),
]


def apply(overrides):
    for k, v in PROD.items():
        setattr(brain, k, overrides.get(k, v))


def restore():
    for k, v in PROD.items():
        setattr(brain, k, v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    header = (f"生产默认：{PROD}\n评测集 {len(ev.CASES)} 条（R1095 扩样后）。\n"
              "消融口径：d=cand−prod；bootstrap 95% 上界<0 才算「关掉显著变差 → 有证据支持」。\n"
              "证据不足 ≠ 无效，本轮不回滚任何默认档。\n")
    try:
        results = ab.run_ab(label="R1096 生产默认档消融体检（Recall@3, 73条）",
                            configs=CONFIGS, apply=apply, restore=restore,
                            root=args.root, out=args.out, cases=ev.CASES,
                            header=header, verbose=False)
    finally:
        restore()

    base = results[0]
    buf = io.StringIO()
    buf.write("R1096 消融裁定表（判据写在前：bootstrap 上界 < 0 才判「有证据支持」）\n")
    buf.write(f"基准 prod：Recall@3={base['hit']}/{base['scored']}={base['rate']}% "
              f"噪声={base['noise']} p50={base['p50_ms']}ms\n\n")
    supported = []
    for r in results[1:]:
        pv = ab.paired_verdict(base["per_case"], r["per_case"])
        dr = round(r["rate"] - base["rate"], 1)
        verdict = ("有证据支持" if pv["boot_hi"] < 0
                   else "证据不足/待确认")
        if pv["boot_hi"] < 0:
            supported.append(r["label"])
        buf.write(f"  {r['label']:<12} Δrate={dr:+5.1f}pp Δ噪声={r['noise']-base['noise']:+3d} "
                  f"Δp50={round(r['p50_ms']-base['p50_ms'],1):+6.1f}ms | "
                  f"变好g={pv['gain_n']} 变差l={pv['loss_n']} "
                  f"McNemar p={pv['mcnemar_p']:.4f} "
                  f"CI=[{pv['boot_lo']:+.4f},{pv['boot_hi']:+.4f}] → {verdict}\n")
    buf.write(f"\n有证据支持的组件：{supported or '无'}\n")
    buf.write("说明：71 条量级下大多数组件单独贡献都在噪声内，这是**功效不足**的信号，"
              "不是「这些组件没用」。删组件属方向级动作，需拍板；本轮只出表。\n")
    text = buf.getvalue()
    print(text)
    if args.out:
        p = Path(args.out)
        p.write_text(p.read_text(encoding="utf-8") + "\n" + text, encoding="utf-8")
        print(f"已写：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
