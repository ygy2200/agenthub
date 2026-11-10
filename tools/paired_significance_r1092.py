# -*- coding: utf-8 -*-
"""R1092：用配对显著性检验复评**全部历史 A/B 结论**（不重跑真库）。

R1091 的外部调研（output/迭代依据-R1091-评测统计方法外部调研-2026-11-10.md）给出硬口径：
  · 40 条评测集上 95% 区间半宽约 ±15pp，点估计 +10pp 根本不显著；
  · 二元 hit@k 应看不一致对做 McNemar / paired bootstrap，而不是比百分点；
  · 「14 比 6 听着很能说服人，其实 p≈0.07」—— 小样本尤其容易被点估计骗。
本项目 R1084 的「+18.2pp、4 条转命中零转丢失」正是同型：22 条上 4 vs 0，
精确 McNemar p=0.125；扩到 40 条后变成 4 胜 1 负，p=0.375。

本工具扫 output/ 下所有含 results 的 A/B json，逐档重算 g/l、McNemar p、
逐例差 bootstrap 95% 区间，把「旧判据 PASS」与「新判据 PASS」并排列出。
**不追溯改写历史证据文件**，只出一张复评表；历史结论一律标注「旧口径」。

外部依据（工程最佳实践，非同行评审论文）：
  https://iotdigitaltwinplm.com/rag-retrieval-evaluation-recall-at-k-ndcg-mrr-ragas-golden-sets-synthetic-queries-pipeline-2026
  https://digiaeon.com/insights/evaluating-non-deterministic-systems
  https://www.tmls.nyc/research/eval-driven-development

用法：
    D:/python311/python.exe tools/paired_significance_r1092.py --out ../output/xxx.txt
"""
import argparse
import io
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
OUT = ROOT_DIR.parent / "output"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import _ab_common as ab  # noqa: E402


def iter_result_files():
    for p in sorted(OUT.glob("迭代依据-*.json")) + sorted(OUT.glob("*.stability.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("results"), list) and d["results"]:
            yield p, d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    buf = io.StringIO()
    buf.write("R1092 历史 A/B 结论配对显著性复评 | 固定 seed=0, n_boot=10000\n")
    buf.write("新判据：Δrate>0 且 Δ噪声≤0 且 Δp50≤50ms 且 bootstrap 下界>0；"
              "McNemar p 只作诊断。\n")
    buf.write("不显著 ≠ 无效，只等于「证据不足，不改默认」；历史证据文件不改写。\n\n")
    total, downgraded = 0, 0
    for p, d in iter_result_files():
        rs = d["results"]
        base = rs[0]
        buf.write(f"■ {p.name}  label={d.get('label', '')}\n")
        buf.write(f"  基准 {base.get('label')} rate={base.get('rate')} "
                  f"n={len(base.get('per_case') or [])}\n")
        for r in rs[1:]:
            v = (r.get("verdict") or {})
            pv = ab.paired_verdict(base.get("per_case") or [],
                                   r.get("per_case") or [])
            dr = v.get("delta_rate", 0)
            dn = v.get("delta_noise", 0)
            dt = v.get("delta_ms", v.get("delta_avg_ms", 0))
            old = bool(v.get("pass"))
            new = (dr > 0 and dn <= 0 and dt <= 50 and pv["boot_lo"] > 0)
            # 两个统计量不一致时显式标「弱」，不许把 bootstrap 下界>0 读成铁证：
            # R1087 的 22 条集上 4 胜 0 负 → CI 下界 0.0455>0，但 McNemar p=0.125。
            if pv["boot_lo"] <= 0:
                strength = "不足"
            elif pv["mcnemar_p"] < 0.05:
                strength = "强"
            else:
                strength = "弱(两统计量不一致)"
            total += 1
            if old and not new:
                downgraded += 1
            buf.write(f"    {str(r.get('label')):<16} Δrate={dr:+5.1f}pp "
                      f"Δ噪声={dn:+3d} Δp50={dt:+6.1f}ms | "
                      f"g={pv['gain_n']} l={pv['loss_n']} "
                      f"p={pv['mcnemar_p']:.4f} "
                      f"CI=[{pv['boot_lo']:+.4f},{pv['boot_hi']:+.4f}] "
                      f"证据={strength} | "
                      f"旧={'PASS' if old else 'KEEP-OFF'} → "
                      f"新={'PASS' if new else 'KEEP-OFF'}"
                      f"{'  ⚠降级' if old and not new else ''}\n")
        buf.write("\n")
    buf.write(f"汇总：复评 {total} 个候选档，其中旧判据 PASS 但新判据降级 "
              f"{downgraded} 个。\n")
    buf.write("口径沿革：R1074–R1087 用 Δ均值、无配对检验；R1090 改 Δp50；"
              "R1092 加配对检验。历史结论一律按「旧口径、证据不足」标注，不追溯改写。\n")
    text = buf.getvalue()
    print(text)
    if args.out:
        q = Path(args.out)
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text(text, encoding="utf-8")
        print(f"已写：{q}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
