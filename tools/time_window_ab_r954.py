# -*- coding: utf-8 -*-
"""R954（B116）：时间限窗的同库 A/B —— 收益、代价、噪声三项同报。

判据（写在前，不许事后放宽，照 R898/R900/R916/R946 的先例）：
    只有「越窗注入下降 ≥50%」且「ΔRecall@3 ≥ 0」且「Δ噪声格 ≤ 0」三条同时成立，
    才允许把 RECALL_TIME_WINDOW 从 0 改成 1。任一不满足 → 维持默认关，
    本轮只交量具与证据，不改生产行为。零收益就整段撤掉，不为指标给护栏开洞。

两侧同一份代码、同一个真库、同一批语料，只切 brain.RECALL_TIME_WINDOW：
  · 收益侧：R948 的 8 组含时间词提问，量「注入里日期越窗的条数」。日期口径
    **必须与生产一致**——用 brain._memory_dates（updated 优先 / created 兜底），
    不沿用 R948 探针的裸 created，否则会造出「实现判窗内、量具判窗外」的假越窗。
  · 代价侧：recall_eval.CASES 常规语料，量 Recall@3 与噪声格；分母锁死
    baseline 判出的题面泄漏样本（R900 教训：分母漂移会凭空造增益）。
  · 附带：报「无时间词提问前后逐位相同」的条数，证明没为一半提问偷偷改行为。
  · 装置样本一律 origin=eval/probe，不进生产分母（记忆 #581）。

只读：不写库、不计唤起（update_state=False）；RECALL_TIME_WINDOW 只在进程内切。
用法：
    D:/python311/python.exe tools/time_window_ab_r954.py
    D:/python311/python.exe tools/time_window_ab_r954.py --out ../output/_r954_time_window_ab.log
"""
import argparse
import sqlite3
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

CONFIGS = [("off", 0), ("on", 1)]


def _record_dates(root: str, ids: list) -> dict:
    """记录行日期（date 列），与记录路 _row_date 同口径，只读连接。"""
    ints = [int(i) for i in (ids or []) if str(i).lstrip("-").isdigit()]
    if not ints:
        return {}
    try:
        conn = sqlite3.connect(brain.db_path(root).as_uri() + "?mode=ro",
                               uri=True, timeout=2.0)
    except (sqlite3.Error, ValueError, OSError):
        return {}
    try:
        ph = ",".join("?" * len(ints))
        return {int(r[0]): str(r[1] or "")[:10] for r in
                conn.execute(f"SELECT id, date FROM records WHERE id IN ({ph})", ints)}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _temporal(root, state, win):
    """收益侧：8 组含时间词提问的越窗注入条数 + 开关生效证据。"""
    brain.RECALL_TIME_WINDOW = win
    outside_mem = outside_rec = injected = dropped = opened = 0
    rows = []
    for i, (timed, _plain) in enumerate(r948.PAIRS, 1):
        window = brain._parse_time_range(timed)
        try:
            res = brain.prompt_recall(
                root, timed, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"r954-{win}-t{i}", state_path=str(state),
                update_state=False, origin="probe")
        except Exception as e:  # noqa: BLE001
            rows.append((timed, "ERROR " + repr(e)))
            continue
        mids = list(res.get("memory_ids") or [])
        rids = list(res.get("record_ids") or [])
        opened += int(bool(res.get("time_window")))
        dropped += int(res.get("time_window_dropped") or 0)
        injected += len(mids) + len(rids)
        lo, hi = window if window else ("", "")
        mdates = brain._memory_dates(root, mids) if window else {}
        rdates = _record_dates(root, rids) if window else {}
        om = sum(1 for x in mids
                 if (d := mdates.get(int(x))) and not (lo <= d <= hi))
        orr = sum(1 for x in rids
                  if (d := rdates.get(int(x))) and not (lo <= d <= hi))
        outside_mem += om
        outside_rec += orr
        rows.append((timed, f"注入 mem{len(mids)}/rec{len(rids)} · "
                            f"越窗 {om}/{orr} · 剔除 {res.get('time_window_dropped') or 0}"))
    return {"outside": outside_mem + outside_rec, "outside_mem": outside_mem,
            "outside_rec": outside_rec, "injected": injected,
            "dropped": dropped, "opened": opened, "rows": rows}


def _regular(root, cases, state, win, skip):
    """代价侧：Recall@3 + 噪声格（分母锁定 baseline 的 leak 样本）。"""
    brain.RECALL_TIME_WINDOW = win
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
                session=f"r954-{win}-{i}", state_path=str(state),
                update_state=False, origin="eval")
        except Exception as e:  # noqa: BLE001
            print(f"  [{i:02d}] ERROR {e!r}")
            continue
        cls, _span, _leak = ev._classify(prompt, accepts, res, root)
        counts[cls] += 1
        got = ([("mem", int(x)) for x in (res.get("memory_ids") or [])[:3]] +
               [("rec", int(x)) for x in (res.get("record_ids") or [])[:3]])
        noise += sum(1 for pair in got if pair not in accepts)
        rows[i] = cls
    scored = sum(counts.values()) - counts["leak"]
    return {"hit": counts["hit"], "scored": scored,
            "rate": counts["hit"] / scored * 100 if scored else 0.0,
            "noise": noise, "rows": rows, "ms": int((time.time() - t0) * 1000)}


def _untouched(root, cases, state, win):
    """无时间词提问：开窗前后注入 id 是否逐位相同。"""
    brain.RECALL_TIME_WINDOW = win
    out = {}
    for i, (prompt, _w, _y) in enumerate(cases, 1):
        if brain._parse_time_range(prompt):
            continue
        try:
            res = brain.prompt_recall(
                root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"r954-{win}-n{i}", state_path=str(state),
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
    say("判据（写在前）：Δ越窗 ≤ -50% 且 ΔRecall@3 ≥ 0 且 Δ噪声格 ≤ 0，三条同时成立"
        "才允许改默认档；否则维持默认关。")

    state = Path(tempfile.mkdtemp(prefix="r954_")) / "state.json"
    base_reg = _regular(root, ev.CASES, state, 0, set())
    leak_ids = {i for i, cls in base_reg["rows"].items() if cls == "leak"}
    if leak_ids:
        base_reg = _regular(root, ev.CASES, state, 0, leak_ids)
    say(f"\n常规语料：总 {len(ev.CASES)} 条 · 题面泄漏剔除 {len(leak_ids)} 条"
        f"（{'、'.join(f'#{i}' for i in sorted(leak_ids)) or '无'}） · 计分分母 {base_reg['scored']}")

    off_untouched = _untouched(root, ev.CASES, state, 0)
    on_untouched = _untouched(root, ev.CASES, state, 1)
    same = sum(1 for k in off_untouched if off_untouched[k] == on_untouched.get(k))
    say(f"无时间词提问：{len(off_untouched)} 条，开窗前后注入逐位相同 {same} 条")

    say("\n档位   越窗注入(记/录)  注入总  开窗次  剔除候选 |  Recall@3   噪声格  耗时ms")
    results = []
    for name, win in CONFIGS:
        t = _temporal(root, state, win)
        r = _regular(root, ev.CASES, state, win, leak_ids)
        results.append((name, t, r))
        say(f"{name:<6} {t['outside']:>3}({t['outside_mem']}/{t['outside_rec']})"
            f" {t['injected']:>8} {t['opened']:>7} {t['dropped']:>9} |"
            f"  {r['hit']:>2}/{r['scored']:<2} {r['rate']:>5.1f}%"
            f" {r['noise']:>7} {r['ms']:>7}")

    say("\n逐组（on 档）：")
    on_t = results[1][1]
    for q, desc in on_t["rows"]:
        say(f"  {q:<30} {desc}")

    off_t, on_t = results[0][1], results[1][1]
    off_r, on_r = results[0][2], results[1][2]
    delta_out = on_t["outside"] - off_t["outside"]
    pct = (delta_out / off_t["outside"] * 100) if off_t["outside"] else 0.0
    d_rate = on_r["hit"] - off_r["hit"]
    d_noise = on_r["noise"] - off_r["noise"]
    say(f"\nΔ越窗 {delta_out:+d}（{pct:+.1f}%） · ΔRecall@3 {d_rate:+d} · "
        f"Δ噪声格 {d_noise:+d} · 无时间词不受影响 {same}/{len(off_untouched)}")

    pass_gain = off_t["outside"] > 0 and pct <= -50.0
    pass_recall = d_rate >= 0
    pass_noise = d_noise <= 0
    verified = on_t["dropped"] > 0 or on_t["opened"] > 0
    say("\n结论：")
    say(f"  开关是否真生效：{'是' if verified else '否'}（开窗 {on_t['opened']} 次、"
        f"剔除候选 {on_t['dropped']} 条）")
    if not verified:
        say("  → 量具或接线没生效，本轮不得下任何收益/代价结论，标待确认。")
    elif off_t["outside"] == 0:
        say("  → 分母为 0（baseline 无越窗注入），方向当前不可验证（记忆 #566 口径），"
            "不得把 0/0 写成 0% 收益。维持默认关。")
    elif pass_gain and pass_recall and pass_noise:
        say("  → 三条判据同时成立，允许把 RECALL_TIME_WINDOW 改成 1（需另轮提交 + 回归）。")
    else:
        say(f"  → 判据未全过：越窗达标={'是' if pass_gain else '否'} / "
            f"Recall@3 达标={'是' if pass_recall else '否'} / "
            f"噪声达标={'是' if pass_noise else '否'}。**维持默认关**，"
            "不为指标给护栏开洞（R946 同款）。")
    say("  口径：装置样本 origin=eval/probe 不进生产分母；越窗日期用 brain._memory_dates"
        "（与生产同口径），记录路用 records.date；分母锁死 baseline 的 leak 样本。")
    if args.out:
        stream.close()
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
