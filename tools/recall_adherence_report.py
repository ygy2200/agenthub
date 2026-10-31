# -*- coding: utf-8 -*-
"""R827：注入采纳率报表——「注入的记忆到底被用了几条」。

    python tools/recall_adherence_report.py            # 近 30 天
    python tools/recall_adherence_report.py --days 7
    python tools/recall_adherence_report.py --mark <session> --text "这一轮的产出…"

口径（为什么这么算，别照着改）：
  · **分母只算可指纹行**。「抽不出指纹」不等于「没被用」，那是「测不了」；把它算进
    分母会系统性压低采纳率，从而让人得出「注入没用」的错误结论。
  · 采纳率对照区间 **40%–70%**（sheratonhq 的实测口径）。低于 40% 是排序/门槛有问题，
    高于 70% 要怀疑上下文冗余（全都用上了，说明给得太少或太泛）。
  · 平均记忆年龄对照 **7–30 天**：太短=只靠近期记忆，太长=旧结论权重过高。
  · 分 strong / weak 两档看——如果两档采纳率差不多，说明 R826 的「分流」判断不成立。

标记采纳（agent 侧调用）：
    --mark <session> --text "<本轮回答或执行的命令>"   → 命中指纹即记 used
"""
import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  控制台 UTF-8 守卫
import brain  # noqa: E402

RATE_LO, RATE_HI = 40.0, 70.0
AGE_LO, AGE_HI = 7.0, 30.0


def _verdict(rate: float, fingerprintable: int) -> str:
    if fingerprintable == 0:
        return "样本不足（还没有可指纹的注入记录）"
    if rate < RATE_LO:
        return "偏低——先看排序（Recall@3）与门槛（_RECALL_RARE_IDF），再看注入文本是否太长被忽略"
    if rate > RATE_HI:
        return "偏高——留意上下文冗余：全都用上往往说明给得太少或太泛"
    return "在区间内"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=brain.ADHERENCE_KEEP_DAYS)
    ap.add_argument("--mark", default="", help="要标记采纳的 session")
    ap.add_argument("--text", default="", help="这一轮的产出文本（用来匹配指纹）")
    ap.add_argument("--root", default="")
    a = ap.parse_args()
    root = a.root or _root()

    if a.mark:
        if not a.text:
            print("× --mark 需要同时给 --text（这一轮的回答或命令）")
            return 2
        r = brain.mark_recall_used(root, a.mark, a.text)
        print(f"检查 {r['checked']} 条注入记录，命中 {r['matched']} 条：{r['memory_ids']}")
        return 0

    st = brain.recall_adherence_stats(root, a.days)
    print(f"窗口：近 {st['window_days']} 天（大脑根：{root}）")
    print(f"注入记录  {st['injected']} 条")
    print(f"可指纹    {st['fingerprintable']} 条"
          + (f"（占 {st['fingerprintable'] / st['injected'] * 100:.0f}%）" if st["injected"] else ""))
    print(f"被用      {st['used']} 条")
    print(f"采纳率    {st['rate']}%（健康区间 {RATE_LO:.0f}%–{RATE_HI:.0f}%）→ {_verdict(st['rate'], st['fingerprintable'])}")
    if st["avg_memory_age_days"] is not None:
        age = st["avg_memory_age_days"]
        note = "偏新（只靠近期记忆）" if age < AGE_LO else (
            "偏旧（旧结论权重可能过高）" if age > AGE_HI else "在区间内")
        print(f"平均记忆年龄 {age} 天（参考 {AGE_LO:.0f}–{AGE_HI:.0f}）→ {note}")
    if st["by_strength"]:
        print("\n分档（R826 的分流判断是否成立，看这两行差距）：")
        for s in ("strong", "weak"):
            b = st["by_strength"].get(s)
            if not b:
                continue
            print(f"  {s:<6} 注入 {b['injected']:>4}  可指纹 {b['fingerprintable']:>4}  "
                  f"被用 {b['used']:>3}  采纳率 {b['rate']}%")
    return 0


def _root() -> str:
    import json
    import os
    env = os.environ.get("AGENTHUB_ROOT")
    if env:
        return env
    cfg = Path.home() / ".agenthub" / "config.json"
    try:
        return str(json.loads(cfg.read_text(encoding="utf-8")).get("root") or "")
    except (OSError, ValueError):
        return ""


if __name__ == "__main__":
    raise SystemExit(main())
