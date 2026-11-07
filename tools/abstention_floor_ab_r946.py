# -*- coding: utf-8 -*-
"""R946（B114）：弃答地板的同库 A/B —— 收益、代价、噪声三项同报。

判据（写在前，不许事后放宽，照 R898/R900/R916 的先例）：
    只有「假阳性下降 ≥50%」且「ΔRecall@3 ≥ 0」且「Δ噪声格 ≤ 0」三条同时成立，
    才允许把 RECALL_ABSTAIN_MIN_COV 从 0 改成该档。任一不满足 → 维持默认关，
    本轮只交量具与证据，不改生产行为。零收益就整段撤掉，不为了指标给护栏开洞。

两侧同一份代码、同一个真库、同一批语料，只切 brain.RECALL_ABSTAIN_MIN_COV：
  · 收益侧：tools/recall_abstention_eval_r945.py 的 12 条样本（有效 9 条，
    contaminated 自动剔除），量「假阳性条数 / 注入记忆数 / 注入记录数」。
  · 代价侧：tools/recall_eval.py 的 32 条常规语料，量 Recall@3 与噪声格；
    分母锁死 baseline 判出的题面泄漏样本（R900 教训：分母漂移会凭空造增益）。
  · 装置样本一律 origin=probe/eval，不进生产分母（记忆 #581）。

用法：
    D:/python311/python.exe tools/abstention_floor_ab_r946.py
    D:/python311/python.exe tools/abstention_floor_ab_r946.py --out ../output/_r946_abstention_ab.log
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
import recall_abstention_eval_r945 as abst  # noqa: E402

CONFIGS = [("baseline", 0.0), ("cov0.20", 0.20), ("cov0.30", 0.30),
           ("cov0.40", 0.40), ("cov0.50", 0.50), ("cov0.60", 0.60),
           ("cov0.70", 0.70)]


def _regular(root, cases, state, cov, skip):
    """常规语料一侧：Recall@3 + 噪声格 + 弃答次数（不许把真答案也弃掉）。"""
    brain.RECALL_ABSTAIN_MIN_COV = cov
    counts = {"hit": 0, "gap": 0, "miss": 0, "leak": 0}
    noise = 0
    abstained = 0
    rows = {}
    state_path = str(state)
    for i, (prompt, want, _why) in enumerate(cases, 1):
        if i in skip:
            continue
        accepts = ev._accepts(want)
        if not accepts:
            continue
        try:
            res = brain.prompt_recall(
                root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"r946-{cov}-{i}", state_path=state_path,
                update_state=False, origin="eval")
        except Exception as e:  # noqa: BLE001
            print(f"  [{i:02d}] ERROR {e!r}")
            continue
        cls, _span, _leak = ev._classify(prompt, accepts, res, root)
        counts[cls] += 1
        abstained += int(res.get("abstained") or 0)
        got = ([("mem", int(x)) for x in (res.get("memory_ids") or [])[:3]] +
               [("rec", int(x)) for x in (res.get("record_ids") or [])[:3]])
        noise += sum(1 for pair in got if pair not in accepts)
        rows[i] = cls
    scored = sum(counts.values()) - counts["leak"]
    return {"hit": counts["hit"], "scored": scored,
            "rate": counts["hit"] / scored * 100 if scored else 0.0,
            "noise": noise, "abstained": abstained, "rows": rows}


def _abstain(root, cov):
    brain.RECALL_ABSTAIN_MIN_COV = cov
    return abst.run(root)


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
    say("判据（写在前）：Δ假阳性 ≤ -50% 且 ΔRecall@3 ≥ 0 且 Δ噪声格 ≤ 0，三条同时成立"
        "才允许改默认档；否则维持默认关。")

    state = Path(tempfile.mkdtemp(prefix="r946_")) / "state.json"

    # 分母锁定：baseline 判 leak 的样本在每一档都跳过（R900）。
    base_reg = _regular(root, ev.CASES, state, 0.0, set())
    leak_ids = {i for i, cls in base_reg["rows"].items() if cls == "leak"}
    if leak_ids:
        base_reg = _regular(root, ev.CASES, state, 0.0, leak_ids)
    say(f"\n常规语料：总 {len(ev.CASES)} 条 · 题面泄漏剔除 {len(leak_ids)} 条"
        f"（{'、'.join(f'#{i}' for i in sorted(leak_ids)) or '无'}） · 计分分母 {base_reg['scored']}")

    base_abs = _abstain(root, 0.0)
    say(f"abstention 样本：总 {base_abs['total']} 条 · 有效 {base_abs['valid']} 条 · "
        f"剔除 {base_abs['excluded']} 条" +
        (f"（{'、'.join(base_abs['contaminated'])}）" if base_abs["contaminated"] else ""))

    say("\n档位        假阳性  注入记忆  注入记录  |  Recall@3   噪声格  真答案被弃  耗时ms")
    results = []
    for name, cov in CONFIGS:
        a = _abstain(root, cov)
        r = _regular(root, ev.CASES, state, cov, leak_ids)
        results.append((name, cov, a, r))
        say(f"{name:<12} {a['injected']:>4}/{a['valid']:<3}  {a['injected_mem']:>6}"
            f"  {a['injected_rec']:>7}  |  {r['hit']:>2}/{r['scored']:<2}"
            f" {r['rate']:>6.1f}%   {r['noise']:>5}   {r['abstained']:>7}      -")

    b_a, b_r = results[0][2], results[0][3]
    say("\n逐档判定：")
    winners = []
    for name, cov, a, r in results[1:]:
        drop = (b_a["injected"] - a["injected"]) / b_a["injected"] * 100 if b_a["injected"] else 0.0
        d_rate = r["rate"] - b_r["rate"]
        d_noise = r["noise"] - b_r["noise"]
        verdict = (drop >= 50.0 and d_rate >= 0 and d_noise <= 0)
        say(f"  {name:<10} 假阳性 {b_a['injected']}→{a['injected']}（降 {drop:.0f}%） · "
            f"ΔRecall@3 {d_rate:+.1f}pp · Δ噪声 {d_noise:+d} · 真答案被弃 {r['abstained']} 条"
            f"  → {'✓ 可改默认档' if verdict else '✗ 不采用'}")
        if verdict:
            winners.append((name, cov, drop, d_rate, d_noise))

    say("\n结论：")
    if winners:
        best = winners[0]
        say(f"  存在安全窗口：{best[0]}（cov={best[1]}，假阳性降 {best[2]:.0f}%、"
            f"ΔRecall@3 {best[3]:+.1f}pp、Δ噪声 {best[4]:+d}）。")
        say("  → 建议把 RECALL_ABSTAIN_MIN_COV 默认值改成该档，并补一条默认档守卫用例；"
            "本量具不改默认值，改动另起一轮。")
    else:
        say("  没有任何档位同时满足三条判据——覆盖率地板与真答案的覆盖率分布重叠，"
            "或收益不足以抵消代价。")
        say("  → **维持默认关**，不把地板塞进生产；R946 只交付量具、诊断字段与证据。"
            "B114 转「待确认」：需要换一路不靠词覆盖的信号（如候选最高绝对分），"
            "禁止为了指标给护栏开洞（R898 先例）。")
    if args.out:
        stream.close()
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
