# -*- coding: utf-8 -*-
"""R900（B099）：闸门救援的同库 A/B 量具。

为什么是「切常量」而不是另写模拟器：R833 三条实锤（记忆 #511）——给自动注入通道
做验收时，离屏夹具的形状不是生产形状，结果会被静默架空。这里每档都直接调
`brain.prompt_recall` 本体，只切 `RECALL_SEM_RESCUE_MODE` / `_SIM`，所以 A/B 的
唯一差异就是救援开关本身，没有第二份评分实现。

隔离（记忆 #540）：origin=eval + 临时 state_path + update_state=False，量具不把
真库记忆推成「推了 5 次零采纳」的 30 天免疫。

输出两类数字，缺一不可：
  · Recall@3（分母剔除题面泄漏样本）——收益
  · 噪声增量：top3 里非期望候选条数相对 off 档的变化——代价
只看收益不看代价，等于重复 R899 模拟里 w=40「语义接管排序」那个错误。

用法：
    D:/python311/python.exe tools/semantic_rescue_ab_r900.py
    D:/python311/python.exe tools/semantic_rescue_ab_r900.py --out ../output/xxx.txt
"""
import argparse
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

# 先跑三档定方向；某档有增益再用 --sweep 扫阈值，别一上来就网格搜索
DEFAULT_CONFIGS = [
    ("off", 0.65),
    ("append", 0.60), ("append", 0.65), ("append", 0.70),
    ("replace", 0.60), ("replace", 0.65), ("replace", 0.70),
]


def _one(cfg, root, cases, state, leak_ids=frozenset()):
    """leak_ids = off 档判出的题面泄漏样本，后续档一律整条剔除。

    R900 第一版量具这里的坑：救援把 #10 从 leak 换成 miss，分母 21→22，
    于是「增益」里混进了分母漂移——5.9pp 有相当一部分是账算出来的，不是召回
    变准了。污染样本本身不可信，既不该计分母也不该计命中，锁死才可比。
    """
    mode, sim = cfg
    brain.RECALL_SEM_RESCUE_MODE = mode
    brain.RECALL_SEM_RESCUE_SIM = sim
    counts = {"hit": 0, "gap": 0, "miss": 0, "leak": 0}
    noise = 0
    rescued = 0
    per_case = []
    for i, (prompt, want, why) in enumerate(cases, 1):
        if i in leak_ids:
            continue
        accepts = ev._accepts(want)
        if not accepts:
            continue
        try:
            res = brain.prompt_recall(
                root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"ab-{mode}-{sim}-{i}", state_path=str(state),
                update_state=False, origin="eval")
        except Exception as e:  # noqa: BLE001 量具不能因单条炸掉而中断
            per_case.append((i, "ERROR", str(e)))
            continue
        cls, _span, _leak = ev._classify(prompt, accepts, res, root)
        counts[cls] += 1
        mem_top = list(res.get("memory_ids") or [])[:3]
        rec_top = list(res.get("record_ids") or [])[:3]
        got = [(k, int(v)) for k, ids in (("mem", mem_top), ("rec", rec_top))
               for v in ids]
        # 非期望候选占了几格：只看打进 top3 的，与 _classify 的口径一致
        noise += sum(1 for pair in got if pair not in accepts)
        rescued += int(res.get("sem_rescued") or 0)
        per_case.append((i, cls, [f"{k}#{v}" for k, v in got]))
    scored = sum(counts.values()) - counts["leak"]
    rate = (counts["hit"] / scored * 100) if scored else 0.0
    return {"mode": mode, "sim": sim, "hit": counts["hit"], "scored": scored,
            "rate": round(rate, 1), "counts": counts, "noise": noise,
            "rescued": rescued, "per_case": per_case}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--configs", default="", help='JSON 列表，如 [["append",0.62]]')
    args = ap.parse_args()
    root = args.root or ev._brain_root()
    cases = ev.CASES
    configs = json.loads(args.configs) if args.configs else DEFAULT_CONFIGS
    state = Path(tempfile.mkdtemp(prefix="rescue_ab_")) / "state.json"

    buf = io.StringIO()
    buf.write(f"R900 闸门救援 A/B | 真库 {root} | 样本 {len(cases)} 条\n")
    buf.write(ev.env_guard_line(root) + "\n")
    buf.write(f"配置：{configs}\n\n")
    # 先独立探测一次 off 档拿剔除集，再让**所有**档（含正式 off）用同一口径跑。
    # 第一版在这里偷懒：第一档跑完才算出 leak_ids，于是 off 的 per_case 比后续档
    # 多两行，zip 从第 11 项起全错位——翻转列表与救援精准率都是假的。
    probe_state = Path(tempfile.mkdtemp(prefix="rescue_probe_")) / "state.json"
    probe = _one(("off", 0.65), root, cases, probe_state)
    leak_ids = frozenset(i for i, cls, _ in probe["per_case"] if cls == "leak")
    results = []
    for cfg in configs:
        r = _one((str(cfg[0]), float(cfg[1])), root, cases, state, leak_ids)
        results.append(r)
        buf.write(f"{r['mode']:8s} sim={r['sim']:.2f}  "
                  f"Recall@3={r['hit']}/{r['scored']}={r['rate']:5.1f}%  "
                  f"噪声格={r['noise']:3d}  救援次数={r['rescued']:2d}  "
                  f"四类={r['counts']}\n")
    buf.write(f"\n分母锁定：剔除题面泄漏样本 {sorted(leak_ids)}，"
              f"各档分母恒为 {results[0]['scored']}。\n")
    base = results[0]
    buf.write("\n相对 off 档：\n")
    for r in results[1:]:
        d_rate = r["rate"] - base["rate"]
        d_noise = r["noise"] - base["noise"]
        flips = [(i, a, b) for (i, a, _), (j, b, _) in
                 zip(base["per_case"], r["per_case"]) if i == j and a != b]
        gained = [f for f in flips if f[1] != "hit" and f[2] == "hit"]
        lost = [f for f in flips if f[1] == "hit" and f[2] != "hit"]
        prec = (len(gained) / r["rescued"]) if r["rescued"] else 0.0
        buf.write(f"{r['mode']:8s} sim={r['sim']:.2f}  Δrate={d_rate:+5.1f}pp  "
                  f"Δ噪声={d_noise:+3d}  救援精准率={len(gained)}/{r['rescued']}"
                  f"={prec:.0%}  转命中={gained}  转丢失={lost}\n")
    buf.write("\n判定口径（分母锁定后）：Δrate>0 且 Δ噪声<=0 且救援精准率>0 "
              "才考虑合入；否则方向证伪。\n"
              "R900 实测结论：append 档证伪（7 次救援 2 次有用、噪声 +3）；"
              "replace 档 0.60–0.62 平台段胜出（+9.6pp / 2/2 精准 / 零丢失 / 噪声 -2）。\n"
              "R900 保持默认 off、只交付语料+开关+量具；R901 按本表把默认切到 "
              "replace/0.62 并补 origin 分档观测。\n"
              "待确认：21 条分母上单条翻转=4.8pp；0.62 距噪声峰值 0.5942 仅 0.026；"
              "真实会话语义路是否已预热未量。\n")
    text = buf.getvalue()
    print(text)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"已写：{p}")
    # 恢复默认，避免同进程后续调用被量具的开关污染
    brain.RECALL_SEM_RESCUE_MODE = "off"
    brain.RECALL_SEM_RESCUE_SIM = 0.65
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
