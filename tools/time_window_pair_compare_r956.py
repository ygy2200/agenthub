# -*- coding: utf-8 -*-
"""R956：off/on 逐组对照 —— 「开窗后零注入」到底是不是开窗造成的。

R954 报「注入总 8→4、5 组零注入」，R955 归因却发现这 5 组的 dropped 全落在
hit 闸门（15/4/19/26/40），而 hit 判定与开窗无关。两个结果摆在一起说明：
**不能把零注入记在开窗头上**，必须逐组比 off 档本来注入几条。

三组分类（互斥、合计 = 有效组数）：
  · already_zero：off=0 且 on=0 → 本来就没答案，与开窗无关（既有 hit 闸门）
  · zeroed：off>0 且 on=0 → 开窗把原本（越窗的）注入删光了，是真代价
  · kept：off>0 且 on>0 → 开窗后仍有窗内答案，是理想行为
另报 off 档每组的越窗注入数，验证「被删的那几条确实越窗」。

只读：update_state=False、origin=probe、只读 SQLite；两档同一份代码/同一个真库，
只切进程内 brain.RECALL_TIME_WINDOW。

用法：
    D:/python311/python.exe tools/time_window_pair_compare_r956.py
    D:/python311/python.exe tools/time_window_pair_compare_r956.py --out ../output/_r956_pair_compare.log
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
import time_window_ab_r954 as ab  # noqa: E402


def _outside(res, root, window):
    """越窗注入条数，口径与 R954 收益侧完全一致。"""
    if not window:
        return 0
    lo, hi = window
    mids = list(res.get("memory_ids") or [])
    rids = list(res.get("record_ids") or [])
    mdates = brain._memory_dates(root, mids)
    rdates = ab._record_dates(root, rids)
    return (sum(1 for x in mids
                if (d := mdates.get(int(x))) and not (lo <= d <= hi)) +
            sum(1 for x in rids
                if (d := rdates.get(int(x))) and not (lo <= d <= hi)))


def _recall(root, prompt, state, i, win):
    brain.RECALL_TIME_WINDOW = win
    return brain.prompt_recall(
        root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
        session=f"r956-{win}-{i}", state_path=str(state),
        update_state=False, origin="probe")


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
    say("逐组 off/on 对照：只切进程内开关，其余全同。")

    state = Path(tempfile.mkdtemp(prefix="r956_")) / "state.json"
    say("\n提问                              off注入  off越窗  on注入  on越窗  分类")
    buckets = {"already_zero": [], "zeroed": [], "kept": []}
    for i, (timed, _plain) in enumerate(r948.PAIRS, 1):
        window = brain._parse_time_range(timed)
        a = _recall(root, timed, state, i, 0)
        b = _recall(root, timed, state, i, 1)
        na = len(a.get("memory_ids") or []) + len(a.get("record_ids") or [])
        nb = len(b.get("memory_ids") or []) + len(b.get("record_ids") or [])
        oa, ob = _outside(a, root, window), _outside(b, root, window)
        if na == 0 and nb == 0:
            cls = "already_zero"
        elif na > 0 and nb == 0:
            cls = "zeroed"
        else:
            cls = "kept"
        buckets[cls].append((timed, na, oa, nb, ob))
        say(f"{timed:<32} {na:>6} {oa:>8} {nb:>7} {ob:>8}  {cls}")

    say(f"\n分类合计（{sum(len(v) for v in buckets.values())} 组）：")
    say(f"  already_zero（本来就没答案，与开窗无关） {len(buckets['already_zero'])} 组")
    say(f"  zeroed（开窗把原有注入删光，真代价）     {len(buckets['zeroed'])} 组")
    say(f"  kept（开窗后仍有窗内答案，理想行为）     {len(buckets['kept'])} 组")
    say("\nzeroed 明细：")
    for timed, na, oa, nb, ob in buckets["zeroed"]:
        say(f"  {timed}：off {na} 条（其中越窗 {oa}）→ on {nb} 条")
    say("\n结论：")
    zeroed = buckets["zeroed"]
    if not zeroed:
        say("  没有任何一组是「开窗前能注入、开窗后变零」→ R954 观察到的 5 组零注入"
            "**不是开窗造成的**，是既有 hit 闸门在宽泛列举型提问上的老问题。"
            "开窗的净效果只是删掉越窗注入，B116 的三条判据结论成立。")
    elif all(oa > 0 for _t, _na, oa, _nb, _ob in zeroed):
        say(f"  {len(zeroed)} 组变零，且 off 档这些组注入的全部/部分本来就越窗 → "
            "删掉的是错误答案，不是有用答案；但「什么都不给」仍是有用性损失，"
            "登记为遗留缺口（窗内候选被 hit 闸门卡死），需另轮设计补位 + 同库 A/B，"
            "**本轮不因此改默认档**。")
    else:
        say(f"  {len(zeroed)} 组变零，其中存在 off 档窗内注入也被删的情况 → "
            "限窗与闸门叠加确实误伤，判据不足，维持默认关并登记缺口。")
    say("  口径：越窗判定用 brain._memory_dates / records.date，与 R954 收益侧一致；"
        "装置样本 origin=probe 不进生产分母。")
    if args.out:
        stream.close()
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
