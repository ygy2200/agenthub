# -*- coding: utf-8 -*-
"""R924：指纹卫生量化——抽出来的招牌里有多少**永远不可能被匹配上**（只读）。

为什么先量：R922 的探针顺手暴露了形状（`…\\skill.md`（作者`、`https://…/generate_204``、
`e:\\recyclebinbackup-20261020；同时决定放弃`），但「有几条、占多少笔」没数过。
采纳账（R827 起的唯一「真被用上」信号）是 substring 命中，带反引号/中文标点的 token
在 agent 输出里不可能原样出现——命中恒为 0，于是采纳率被**系统性压低**，而且 R923 刚把
简报规则接进这本账，被压低的正是最需要看得准的那一档（required）。

判据写在前面（跑完不挑口径）：
  · BAD_SHARE_MIN = 0.10：不可匹配 token 占全部 token 的比例 ≥10%，或受影响记忆条数 ≥5
    → 值得动 `recall_fingerprints`（只做净化，不放宽任何阈值）；
  · 都不到就是 hygiene_ok：别为此改判定尺子。

用法：python tools/fp_hygiene_probe_r924.py [--sample 500]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  Windows 管道默认 cp936，中文招牌词会 UnicodeEncodeError
import brain  # noqa: E402
import core  # noqa: E402

# 判定用的「不可能出现在输出里」字符集：反引号/引号/中文全角标点/空白。
# ASCII 的 `:` `.` `/` `\` `-` `_` 不算——路径、URL、文件名本来就带它们。
NEVER_MATCH = re.compile(r"[`'\"“”‘’（）()【】「」〔〕，。、；：！？」…\s]")
BAD_SHARE_MIN = 0.10
BAD_MEMORIES_MIN = 5


def rows_of(root: str, sample: int) -> list:
    with brain.db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT memory_id, origin, fp, used FROM recall_adherence"
            " ORDER BY ts DESC LIMIT ?", (sample,))]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="指纹卫生量化（只读）")
    ap.add_argument("--root", default="")
    ap.add_argument("--sample", type=int, default=500)
    args = ap.parse_args(argv)
    root = (args.root or (core.load_config() or {}).get("root", "") or "").strip()
    if not root:
        print("读不到大脑根目录")
        return 1

    rows = rows_of(root, args.sample)
    total = bad = 0
    bad_memories, bad_examples, used_with_bad = set(), [], 0
    for r in rows:
        try:
            fps = json.loads(r["fp"] or "[]")
        except ValueError:
            continue
        hit = False
        for t in fps:
            total += 1
            if NEVER_MATCH.search(t):
                bad += 1
                hit = True
                if len(bad_examples) < 12:
                    bad_examples.append((r["memory_id"], t))
        if hit:
            bad_memories.add(r["memory_id"])
            if int(r["used"] or 0):
                used_with_bad += 1

    share = (bad / total) if total else 0.0
    print(f"近 {len(rows)} 笔注入 · token {total} 个 · 不可匹配 {bad} 个（{share * 100:.1f}%）")
    print(f"受影响记忆 {len(bad_memories)} 条 · 其中仍被判「用上」的笔数 {used_with_bad}"
          f"（这些是靠别的 token 命中的）")
    print("\n样本（memory_id, token）：")
    for mid, t in bad_examples:
        print(f"  #{mid}  {t!r}")
    verdict = ("needs_sanitizing" if (share >= BAD_SHARE_MIN
             or len(bad_memories) >= BAD_MEMORIES_MIN) else "hygiene_ok")
    print(f"\n判定：{verdict}（判据写在前：占比≥{BAD_SHARE_MIN:.0%} 或受影响≥{BAD_MEMORIES_MIN} 条）")
    print(f"口径提醒：净化只改「token 怎么切」，不动长度下限/停用词/池内 df/提问去重这四道过滤，"
          f"更不动命中判据——放宽闸门不在这轮的自由度里。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
