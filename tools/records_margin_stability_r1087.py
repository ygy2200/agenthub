# -*- coding: utf-8 -*-
"""R1087：R1086 的 PASS 档是不是贴着 22 条样本挑出来的？两道稳健性检验。

R1086 结果：raw/cap2/m.90 相对生产现状 +18.2pp / Δ噪声 −2 / Δ耗时 +28.5ms，
4 条转命中零转丢失，满足预注册判据。但：
  ① 22 条样本上 1 条翻转 = 4.5pp，0.90 是扫出来的档，可能是单点运气；
  ② R1074 有前车之鉴：「贴着边界选档」（gate 0.70 与 #13 的 0.75 只差 0.05），
     扩样后立刻回落。选档前必须看**平台**而不是最优点。

两道检验（都写在前，不许事后挑）：
  A. margin 平台扫描 0.62→0.98 共 9 档：若 0.85–0.95 是一片连续的
     「+18.2pp 且噪声≤0」平台，取平台中位 0.90；若只有单点过线，判过拟合，
     默认档维持 off。
  B. 分半检验：按样本编号奇/偶分两半，各自算 raw/m.90 相对 off 的 Δrate 与
     Δ噪声；要求**两半都 Δrate>0**，否则增益由少数样本撑着，不改名默认。
  另记一条耗时复跑：R1086 各档耗时都 +20~30ms，怀疑是基准档首跑缓存冷，
  本轮把 off 再跑一次取两次中位数，避免把预热差当改动成本。

用法：
    D:/python311/python.exe tools/records_margin_stability_r1087.py --out ../output/xxx.txt
"""
import argparse
import io
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import _ab_common as ab  # noqa: E402
import records_recall_eval as rce  # noqa: E402

MARGINS = (0.62, 0.70, 0.75, 0.80, 0.85, 0.88, 0.90, 0.92, 0.95, 0.98)
CONFIGS = [("off", ("off", 2, 0.0))] + [
    (f"raw/m{m:.2f}", ("raw", 2, m)) for m in MARGINS]
_ORIG = (brain.RECALL_REC_QUERY_MODE, brain.RECALL_RECORD_CAP, brain.RECALL_REC_MARGIN)


def apply(cfg):
    mode, cap, margin = cfg
    brain.RECALL_REC_QUERY_MODE = mode
    brain.RECALL_RECORD_CAP = cap
    brain.RECALL_REC_MARGIN = margin


def restore():
    brain.RECALL_REC_QUERY_MODE, brain.RECALL_RECORD_CAP, brain.RECALL_REC_MARGIN = _ORIG


def _noise_of(case, got):
    want = rce.accepts_of(case[1])
    return sum(1 for _k, v in got if int(v) not in want)


def split_half(results, out):
    """按编号奇偶分半，用 per_case 重算 rate/噪声（无需重跑真库）。"""
    base = next(r for r in results if r["label"] == "off")
    best = next((r for r in results if r["label"] == "raw/m0.90"), None)
    if not best:
        out.write("分半检验：缺 raw/m0.90 档，跳过\n")
        return False
    bm = {i: (cls, got) for i, cls, got in base["per_case"]}
    ok = True
    for name, sel in (("奇数半", lambda i: i % 2 == 1), ("偶数半", lambda i: i % 2 == 0)):
        rows = [(i, bm.get(i)) for i in range(1, len(rce.CASES_REC) + 1) if sel(i)]
        n = len(rows)
        b_hit = sum(1 for i, v in rows if v and v[0] == "hit")
        t_hit = sum(1 for i, cls, got in best["per_case"] if sel(i) and cls == "hit")
        b_noise = sum(
            _noise_of(rce.CASES_REC[i - 1],
                      [(g.split("#")[0], int(g.split("#")[1]))
                       for g in (bm[i][1] if bm.get(i) else [])])
            for i, _ in rows if bm.get(i))
        t_noise = sum(_noise_of(rce.CASES_REC[i - 1],
                                [(g.split("#")[0], int(g.split("#")[1])) for g in got])
                      for i, cls, got in best["per_case"] if sel(i))
        dr = round((t_hit - b_hit) / n * 100, 1)
        dn = t_noise - b_noise
        good = dr > 0
        ok = ok and good
        out.write(f"  {name}（n={n}）：off {b_hit}/{n} → raw/m.90 {t_hit}/{n}，"
                  f"Δrate={dr:+.1f}pp · Δ噪声={dn:+d} · {'PASS' if good else 'FAIL'}\n")
    out.write(f"  分半判据：两半都要 Δrate>0 → {'通过' if ok else '不通过'}\n")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    buf = io.StringIO()
    buf.write("R1087 稳健性检验 | margin 平台 + 分半 | 基准固定生产现状 off/cap2/m.62\n")
    buf.write("平台判据：0.85–0.95 须连续满足 Δrate>0 且 Δ噪声≤0 且 Δ耗时≤50ms；"
              "只单点过线判过拟合。\n\n")
    header = ("预注册：Δrate>0 且 Δ噪声≤0 且 Δ耗时≤50ms 相对 off；"
              "本轮目的是看平台宽度，不是再挑一个更好的点。\n")
    try:
        results = ab.run_ab(label="R1087 记录路 margin 平台扫描（Recall@2）",
                            configs=CONFIGS, apply=apply, restore=restore,
                            root=args.root, out=args.out, cases=rce.CASES_REC,
                            judge=ab.records_judge, metric="Recall@2",
                            header=header, verbose=False)
    finally:
        restore()
    base = results[0]
    buf.write("平台表（相对 off）：\n")
    plate = []
    for r in results[1:]:
        v = r["verdict"]
        buf.write(f"  {r['label']:<12} rate={r['rate']:5.1f}%  "
                  f"Δrate={v['delta_rate']:+5.1f}pp  Δ噪声={v['delta_noise']:+3d}  "
                  f"Δ耗时={v['delta_ms']:+6.1f}ms  "
                  f"{'PASS' if v['pass'] else 'KEEP-OFF'}\n")
        if v["pass"]:
            plate.append(r["label"])
    buf.write(f"\n通过档：{plate}\n")
    core = [x for x in plate if any(f"m{m:.2f}" in x for m in (0.85, 0.88, 0.90, 0.92, 0.95))]
    buf.write(f"0.85–0.95 区间通过 {len(core)}/5 档：{core}\n")
    buf.write(f"平台判据：{'通过（取 0.90）' if len(core) >= 3 else '不通过（视为过拟合，默认维持 off）'}\n\n")
    buf.write("分半检验（raw/m0.90 vs off）：\n")
    half_ok = split_half(results, buf)
    buf.write("\n结论（写在前）：只有「平台≥3/5 且分半两半均 Δrate>0」同时成立，"
              "R1088 才允许把默认档改成 raw + m0.90。\n")
    text = buf.getvalue()
    print(text)
    if args.out:
        p = Path(args.out)
        p.write_text(p.read_text(encoding="utf-8") + "\n" + text, encoding="utf-8")
        Path(str(p) + ".stability.json").write_text(json.dumps(
            {"plate": plate, "core": core, "half_ok": half_ok,
             "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已写：{args.out} (+ .stability.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
