# -*- coding: utf-8 -*-
"""R961（B118）：窗内命中门槛放宽的同库 A/B —— 收益、代价、越窗、噪声四项同报。

判据（写在前，不许事后放宽，照 R946/R954 先例），四条同时成立才准把
RECALL_TIME_HIT_RELAX 从 0 改成 1：
  ① Δ零注入组数 ≤ -2（R948 的 8 组时间词提问里至少 2 组从「什么都不给」变有答案）
  ② ΔRecall@3 ≥ 0（32 条常规语料，分母锁死 baseline 的 leak）
  ③ Δ噪声格 ≤ 0
  ④ on 档越窗注入仍为 0（放宽不许把窗外候选放回来）
任一不满足 → 维持默认关，本轮只交量具与证据，不改生产行为。

两侧同一份代码、同一个真库、同一批语料，只切 brain.RECALL_TIME_HIT_RELAX
（RECALL_TIME_WINDOW 已是默认 1，R957 合入）。日期口径与生产/R954 完全一致：
brain._memory_dates（updated 优先）+ records.date。装置样本 origin=eval/probe。

用法：
    D:/python311/python.exe tools/time_hit_relax_ab_r961.py
    D:/python311/python.exe tools/time_hit_relax_ab_r961.py --out ../output/_r961_hit_relax_ab.log
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  R940：tools/ 新脚本必须装控制台 UTF-8 守卫
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402
import time_aware_probe_r948 as r948  # noqa: E402
import time_window_pair_compare_r956 as pair  # noqa: E402

CONFIGS = [("off", 0), ("on", 1)]


def _temporal(root, state, relax):
    """收益侧：8 组含时间词提问的零注入组数 / 注入量 / 越窗 / 放宽触发。"""
    brain.RECALL_TIME_HIT_RELAX = relax
    zero = 0
    injected = outside = relaxed = 0
    rows = []
    for i, (timed, _plain) in enumerate(r948.PAIRS, 1):
        window = brain._parse_time_range(timed)
        try:
            res = brain.prompt_recall(
                root, timed, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"r961-{relax}-t{i}", state_path=str(state),
                update_state=False, origin="probe")
        except Exception as e:  # noqa: BLE001
            rows.append((timed, "ERROR " + repr(e)))
            continue
        n = len(res.get("memory_ids") or []) + len(res.get("record_ids") or [])
        if n == 0:
            zero += 1
        injected += n
        outside += pair._outside(res, root, window)
        relaxed += int(res.get("time_hit_relaxed") or 0)
        rows.append((timed, f"注入 {n} · 放宽进池 {res.get('time_hit_relaxed') or 0} · "
                            f"剔除 {res.get('time_window_dropped') or 0}"))
    return {"zero": zero, "injected": injected, "outside": outside,
            "relaxed": relaxed, "rows": rows}


def _regular(root, cases, state, relax, skip):
    brain.RECALL_TIME_HIT_RELAX = relax
    counts = {"hit": 0, "gap": 0, "miss": 0, "leak": 0}
    noise = 0
    rows = {}
    t0 = time.time()
    for i, (prompt, want, _why) in enumerate(cases, 1):
        if i in skip:
            continue
        accepts = ev._accepts(want)
        if not accepts:
            continue
        try:
            res = brain.prompt_recall(
                root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"r961-{relax}-{i}", state_path=str(state),
                update_state=False, origin="eval")
        except Exception as e:  # noqa: BLE001
            print(f"  [{i:02d}] ERROR {e!r}")
            continue
        cls, _span, _leak = ev._classify(prompt, accepts, res, root)
        counts[cls] += 1
        got = ([("mem", int(x)) for x in (res.get("memory_ids") or [])[:3]] +
               [("rec", int(x)) for x in (res.get("record_ids") or [])[:3]])
        noise += sum(1 for p in got if p not in accepts)
        rows[i] = cls
    scored = sum(counts.values()) - counts["leak"]
    return {"hit": counts["hit"], "scored": scored,
            "rate": counts["hit"] / scored * 100 if scored else 0.0,
            "noise": noise, "rows": rows, "ms": int((time.time() - t0) * 1000)}


def _untouched(root, cases, state, relax):
    """无时间词提问：开关前后注入 id 是否逐位相同。"""
    brain.RECALL_TIME_HIT_RELAX = relax
    out = {}
    for i, (prompt, _w, _y) in enumerate(cases, 1):
        if brain._parse_time_range(prompt):
            continue
        try:
            res = brain.prompt_recall(
                root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"r961-{relax}-n{i}", state_path=str(state),
                update_state=False, origin="eval")
        except Exception:  # noqa: BLE001
            continue
        out[i] = (list(res.get("memory_ids") or []),
                  list(res.get("record_ids") or []))
    return out


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
    say("判据（写在前）：Δ零注入组数 ≤ -2 且 ΔRecall@3 ≥ 0 且 Δ噪声格 ≤ 0 "
        "且 on 档越窗注入 = 0，四条同时成立才改默认档。")

    state = Path(tempfile.mkdtemp(prefix="r961_")) / "state.json"
    base_reg = _regular(root, ev.CASES, state, 0, set())
    leak_ids = {i for i, cls in base_reg["rows"].items() if cls == "leak"}
    if leak_ids:
        base_reg = _regular(root, ev.CASES, state, 0, leak_ids)
    say(f"\n常规语料：总 {len(ev.CASES)} 条 · 题面泄漏剔除 {len(leak_ids)} 条 · "
        f"计分分母 {base_reg['scored']}")

    off_u = _untouched(root, ev.CASES, state, 0)
    on_u = _untouched(root, ev.CASES, state, 1)
    same = sum(1 for k in off_u if off_u[k] == on_u.get(k))
    say(f"无时间词提问：{len(off_u)} 条，开关前后注入逐位相同 {same} 条")

    say("\n档位  零注入组  注入总  越窗  放宽进池 |  Recall@3    噪声格  耗时ms")
    results = []
    for name, relax in CONFIGS:
        t = _temporal(root, state, relax)
        r = _regular(root, ev.CASES, state, relax, leak_ids)
        results.append((name, t, r))
        say(f"{name:<5} {t['zero']:>8} {t['injected']:>7} {t['outside']:>5}"
            f" {t['relaxed']:>9} |  {r['hit']:>2}/{r['scored']:<2}"
            f" {r['rate']:>5.1f}% {r['noise']:>8} {r['ms']:>8}")

    say("\n逐组（on 档）：")
    for q, desc in results[1][1]["rows"]:
        say(f"  {q:<30} {desc}")

    off_t, on_t = results[0][1], results[1][1]
    off_r, on_r = results[0][2], results[1][2]
    d_zero = on_t["zero"] - off_t["zero"]
    d_rate = on_r["hit"] - off_r["hit"]
    d_noise = on_r["noise"] - off_r["noise"]
    say(f"\nΔ零注入组 {d_zero:+d} · Δ注入总 {on_t['injected'] - off_t['injected']:+d} · "
        f"ΔRecall@3 {d_rate:+d} · Δ噪声格 {d_noise:+d} · 无时间词不受影响 {same}/{len(off_u)}")

    p1 = d_zero <= -2
    p2 = d_rate >= 0
    p3 = d_noise <= 0
    p4 = on_t["outside"] == 0
    verified = on_t["relaxed"] > 0
    say("\n结论：")
    say(f"  开关是否真生效：{'是' if verified else '否'}（放宽进池 {on_t['relaxed']} 条）")
    if not verified:
        say("  → 开关没生效，本轮不得下任何收益/代价结论，标待确认。")
    elif p1 and p2 and p3 and p4:
        say("  → 四条判据同时成立，允许把 RECALL_TIME_HIT_RELAX 改成 1（需另轮提交 + 回归）。")
    else:
        say(f"  → 判据未全过：零注入达标={'是' if p1 else '否'} / "
            f"Recall@3 达标={'是' if p2 else '否'} / 噪声达标={'是' if p3 else '否'} / "
            f"越窗达标={'是' if p4 else '否'}。**维持默认关**，"
            "B118 转「需另设计」，不为指标给护栏开洞（R946 同款）。")
    say("  口径：装置样本 origin=eval/probe 不进生产分母；越窗日期用 brain._memory_dates / "
        "records.date；分母锁死 baseline 的 leak 样本；RECALL_TIME_WINDOW 两档均为 1。")
    if args.out:
        stream.close()
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
