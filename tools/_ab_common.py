# -*- coding: utf-8 -*-
"""R1079：把 R1063–R1078 十几个 A/B 量具里重复的骨架抽成一份。

之前每轮做一次参数扫描都要重写：真库快照隔离、题面泄漏锁分母、四类计数、
噪声格、耗时、相对基准的翻转清单、判据。R1074/R1077 的脚本几乎逐行相同，
差别只在「切哪个常量」——重复代码本身就是隐患：某一轮忘了快照隔离，注入计数
就会串到下一档，结论全废（R1074 注释里写明的纪律）。

纪律（沿用 R1063/R1074，写在这里而不是靠每轮自觉）：
  · configs[0] 必须是「现状/off」档；
  · 每档一份独立真库快照（_abroot.snapshot_root），否则上一档的 inject_count
    会进下一档的排序分；
  · 分母由基准档的 leak 判定锁定，各档同分母；
  · 判据写死：Δrate>0 且 Δ噪声<=0 且 Δ耗时<=50ms 才考虑改默认档；
  · 结果同时落 .json（机器可读，供后续轮次复算，不用重跑真库）。

apply(value) 负责切常量，restore() 负责还原；两者都由调用方给，框架不猜
调用方改了什么。任何异常都在 finally 里还原并放下快照。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import _abroot  # noqa: E402
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402


def default_judge(root, case, res):
    """记忆路口径：Recall@3，噪声 = top3 里非合格答案的格数。"""
    prompt, want, _why = case
    accepts = ev._accepts(want)
    cls, _span, _leak = ev._classify(prompt, accepts, res, root)
    got = [(k, int(v)) for k, ids in
           (("mem", list(res.get("memory_ids") or [])[:3]),
            ("rec", list(res.get("record_ids") or [])[:3]))
           for v in ids]
    return cls, sum(1 for pair in got if pair not in accepts), got


def records_judge(root, case, res):
    """记录路口径（R1084）：只看 record_ids 前 RECALL_RECORD_CAP 条。

    与记忆路分开是硬要求：R1082 实锤 RECALL_RECORD_CAP=2 且两路独立，
    拿记忆路的 top3/合格集口径量记录路等于量了个不存在的东西。
    """
    prompt, target, _why = case
    # R1085：target 允许是合格集（同一任务的待办/结晶两条都算答对）。
    want = ({int(v) for v in target} if isinstance(target, (tuple, list, set))
            else {int(target)})
    cap = max(1, int(brain.RECALL_RECORD_CAP))
    ids = [int(v) for v in (res.get("record_ids") or [])[:cap]]
    got = [("rec", v) for v in ids]
    hit_n = sum(1 for v in ids if v in want)
    if hit_n:
        return "hit", len(ids) - hit_n, got
    cand = [v for v in ids if v not in want]
    worst = 0
    for cid, body in ev._bodies(root, "rec", cand).items():
        worst = max(worst, ev.leak_span(prompt, body))
    if worst >= ev.leak_floor(prompt):
        return "leak", len(ids), got
    return ("gap" if not ids else "miss"), len(ids), got


def _one(root, cases, state, leak_ids, label, judge):
    counts = defaultdict(int)
    noise, elapsed, per_case = 0, [], []
    for i, case in enumerate(cases, 1):
        if i in leak_ids:
            continue
        prompt = case[0]
        t0 = time.perf_counter()
        try:
            res = brain.prompt_recall(
                root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"ab-{label}-{i}", state_path=str(state),
                update_state=False, origin="eval")
        except Exception as exc:  # noqa: BLE001 单条异常不中断量具
            per_case.append((i, "ERROR", [str(exc)[:120]]))
            continue
        elapsed.append((time.perf_counter() - t0) * 1000)
        cls, n_noise, got = judge(root, case, res)
        counts[cls] += 1
        noise += n_noise
        per_case.append((i, cls, [f"{k}#{v}" for k, v in got]))
    scored = sum(counts.values()) - counts["leak"]
    rate = (counts["hit"] / scored * 100) if scored else 0.0
    return {"label": label, "hit": counts["hit"], "scored": scored,
            "rate": round(rate, 1), "counts": dict(counts), "noise": noise,
            "avg_ms": round(sum(elapsed) / len(elapsed) if elapsed else 0.0, 1),
            "per_case": per_case}


def run_ab(*, label: str, configs: list, apply, restore,
           root: str = "", out: str = "", cases=None, judge=None,
           header: str = "", verbose: bool = True, metric: str = "Recall@3"):
    """configs: [(tag, value), ...]，configs[0] 必须是现状档。返回 results 列表。

    R1084：judge 可换成 records_judge 量记录路，cases 传 records_recall_eval.CASES_REC。
    默认仍是记忆路口径，R1079/R1080 的历史结论不受影响。
    """
    root = root or ev._brain_root()
    cases = cases if cases is not None else ev.CASES
    judge = judge or default_judge
    buf = io.StringIO()
    buf.write(f"{label} | 真库 {root} | 样本 {len(cases)} 条 | 档位 {[c[0] for c in configs]}\n")
    buf.write(ev.env_guard_line(root) + "\n")
    if header:
        buf.write(header + "\n")
    buf.write("\n")

    results = []
    try:
        snap0 = _abroot.snapshot_root(root)
        try:
            apply(configs[0][1])
            probe = _one(snap0, cases,
                         Path(tempfile.mkdtemp(prefix="ab_probe_")) / "state.json",
                         frozenset(), str(configs[0][0]), judge)
        finally:
            _abroot.drop_root(snap0)
        leak_ids = frozenset(i for i, cls, _ in probe["per_case"] if cls == "leak")
        state = Path(tempfile.mkdtemp(prefix="ab_state_")) / "state.json"
        for tag, val in configs:
            snap = _abroot.snapshot_root(root)
            try:
                apply(val)
                r = _one(snap, cases, state, leak_ids, str(tag), judge)
            finally:
                _abroot.drop_root(snap)
            results.append(r)
            buf.write(f"{str(tag):<16} {metric}={r['hit']}/{r['scored']}="
                      f"{r['rate']:5.1f}%  噪声格={r['noise']:3d}  "
                      f"均耗时={r['avg_ms']:6.1f}ms  四类={r['counts']}\n")
        base = results[0]
        buf.write(f"\n分母锁定：剔除题面泄漏样本 {sorted(leak_ids)}，"
                  f"各档分母恒为 {base['scored']}。\n")
        buf.write("相对现状档：\n")
        for r in results[1:]:
            flips = [(i, a, b) for (i, a, _), (j, b, _) in
                     zip(base["per_case"], r["per_case"]) if i == j and a != b]
            gained = [f for f in flips if f[1] != "hit" and f[2] == "hit"]
            lost = [f for f in flips if f[1] == "hit" and f[2] != "hit"]
            dr = round(r["rate"] - base["rate"], 1)
            dn = r["noise"] - base["noise"]
            dt = round(r["avg_ms"] - base["avg_ms"], 1)
            ok = dr > 0 and dn <= 0 and dt <= 50
            r["verdict"] = {"delta_rate": dr, "delta_noise": dn,
                            "delta_ms": dt, "gained": gained, "lost": lost,
                            "pass": ok}
            buf.write(f"  {str(r['label']):<16} Δrate={dr:+5.1f}pp  "
                      f"Δ噪声={dn:+3d}  Δ耗时={dt:+6.1f}ms  "
                      f"{'PASS' if ok else 'KEEP-OFF'}  "
                      f"转命中={gained}  转丢失={lost}\n")
        buf.write("\n判据（写在前）：Δrate>0 且 Δ噪声<=0 且 Δ耗时<=50ms 才考虑改默认档。\n")
    finally:
        restore()

    text = buf.getvalue()
    if verbose:
        print(text)
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        Path(str(p) + ".json").write_text(
            json.dumps({"label": label, "results": results}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        if verbose:
            print(f"已写：{p} (+ .json)")
    return results
