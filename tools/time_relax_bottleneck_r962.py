# -*- coding: utf-8 -*-
"""R962：B118 下游瓶颈归因 —— 放宽进池 143 条，为什么只多注入 3 条？

R961 A/B：RECALL_TIME_HIT_RELAX 开后 8 组共 143 条候选跨过 hit 闸门进打分池，
但注入总只从 3 涨到 6、零注入组只从 6 降到 5（判据要降 ≥2）。收益被下游吃掉。
本探针只读取证，把 143 条的去向按层拆开，回答「下一刀该动哪」：
  · dropped.rare 增量 → 稀有词闸门；
  · dropped.cap 增量 → 相对 margin / 选取上限；
  · immune / conf → 免疫与置信；
  · 三者都不涨但注入没涨 → 候选进了 strong 却没被 _recall_pick_margin 选中。
只读：update_state=False、origin=probe；两档只切进程内开关。

用法：
    D:/python311/python.exe tools/time_relax_bottleneck_r962.py
    D:/python311/python.exe tools/time_relax_bottleneck_r962.py --out ../output/_r962_bottleneck.log
"""
import argparse
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  R940：tools/ 新脚本必须装控制台 UTF-8 守卫
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402
import time_aware_probe_r948 as r948  # noqa: E402

KEYS = ("hit", "rare", "immune", "cap", "conf")


def _run(root, state, relax):
    brain.RECALL_TIME_HIT_RELAX = relax
    total = {k: 0 for k in KEYS}
    rows = []
    injected = relaxed = 0
    for i, (timed, _plain) in enumerate(r948.PAIRS, 1):
        try:
            res = brain.prompt_recall(
                root, timed, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"r962-{relax}-{i}", state_path=str(state),
                update_state=False, origin="probe")
        except Exception as e:  # noqa: BLE001
            rows.append((timed, "ERROR " + repr(e)))
            continue
        d = res.get("dropped") or {}
        for k in KEYS:
            total[k] += int(d.get(k, 0))
        n = len(res.get("memory_ids") or []) + len(res.get("record_ids") or [])
        injected += n
        relaxed += int(res.get("time_hit_relaxed") or 0)
        rows.append((timed, n, {k: d.get(k, 0) for k in KEYS},
                     res.get("time_hit_relaxed") or 0))
    return {"total": total, "injected": injected, "relaxed": relaxed, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = args.root or ev._brain_root()
    stream = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout

    def say(s=""):
        print(s, file=stream)

    fp = ev.env_fingerprint(root)
    ok, why = ev.production_parity(fp)
    say(f"环境指纹 env={ev.env_key(fp)} · db={ev.db_key(fp)}"
        f"（memories {fp['memories']} / records {fp['records']}）")
    say(f"生产同构：{'是' if ok else '否'}（{why}）")
    say("只读取证：放宽跨过 hit 闸门的候选，最终死在哪一层。")

    state = Path(tempfile.mkdtemp(prefix="r962_")) / "state.json"
    off = _run(root, state, 0)
    on = _run(root, state, 1)
    say("\n提问                              off注入 → on注入   on 放宽进池"
        "   on dropped(hit/rare/immune/cap/conf)")
    for (q, n0, _d0, _r0), (_q2, n1, d1, r1) in zip(off["rows"], on["rows"]):
        ds = "/".join(str(d1[k]) for k in KEYS)
        say(f"{q:<32} {n0:>3} → {n1:<3} {r1:>12}   {ds}")

    say(f"\n合计：注入 {off['injected']} → {on['injected']}（+{on['injected']-off['injected']}）"
        f" · 放宽进池 {on['relaxed']} 条")
    say("dropped 四类增量（on - off）：")
    deltas = {}
    for k in KEYS:
        d = on["total"][k] - off["total"][k]
        deltas[k] = d
        say(f"  {k:<8} {off['total'][k]:>4} → {on['total'][k]:<4}  Δ{d:+d}")

    say("\n结论：")
    grew = {k: v for k, v in deltas.items() if v > 0}
    if not grew:
        say("  四类 dropped 都没涨，说明放宽进池的候选大多进了 strong，"
            "是 _recall_pick_margin 的相对阈值（0.62）把它们挡在注入之外"
            "——这是**排序选择器**问题，不是准入闸门问题。")
    elif max(grew, key=grew.get) == "rare":
        say("  增量主要落在 rare（稀有词闸门）：放宽进池的候选只命中泛词，"
            "被稀有词线判弱。下一刀得动 rare 口径，但 R919 已把词法侧三条补位"
            "方向全部证伪，别重开。")
    else:
        top = max(grew, key=grew.get)
        say(f"  增量主要落在 {top}：见上表逐层分布。")
    say("  按 R919 结论：词法侧的稀有词闸门删掉的候选，靠启发式补位救不回来；"
        "要么走语义路，要么回头查标注/语料质量。**B118 词法侧不建议再开新旋钮**，"
        "本批维持 RECALL_TIME_HIT_RELAX=0，把方向转「语义路 / 语料质量」待拍板。")
    if args.out:
        stream.close()
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
