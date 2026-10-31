# -*- coding: utf-8 -*-
"""R825：回忆质量量具 —— Recall@3 基线。

为什么要有这个文件：R820 落地「逐句自动回忆」后，只有「能不能召回」的用例，
没有「召回得准不准」的数字。用户 2026-10-31 的反馈是「完全不智能」，而实测
那次提问抽出的检索词里混着「围绕/重点/不用」三个虚词（journal#8421），命中的
是三条泛化元记忆——**召回在跑，但召回的不是用户问的那件事**。没有量具就无法
判断改动是变好还是变坏，这条就是那个量具。

用法：
    python tools/recall_eval.py            # 跑基线，打印逐条 Recall@3 与总分
    python tools/recall_eval.py -v         # 同时打印每条召回的 top3 id

口径：
  每条样本给一句真实会出现的提问 + 一个「必须命中」的记忆 id（人工标注，来自
  本仓真实历史）。命中位置 ≤3 记 1 分，>3 或未命中记 0 分。总分 = 命中率。
  只测排序质量，不测召回上限——`search_memories(limit=40)` 拿回的候选池是固定的。
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  控制台 UTF-8 守卫
import brain  # noqa: E402

# (提问, 期望命中, 说明)；期望写成 ("mem", id) 或 ("rec", id)——
# 这条路本来就是记忆+记录两路，只量记忆会把「过程类问题」误判成漏召回。
CASES = [
    ("release.bat 发布失败分支括号没转义怎么修的", ("mem", 487), "批处理括号块先解析"),
    ("GUI 回归三层全红是不是代码问题", ("mem", 457), "回归口径 tests/run_all.py"),
    ("窗口测试崩在 teardown 是什么原因", ("mem", 463), "裸窗口无父顶层控件"),
    ("SQLite 切页慢，包了表达式的列为什么不走索引", ("mem", 460), "表达式索引"),
    ("exe 里怎么确认打进去的是哪个版本", ("mem", 471), "PyInstaller PYZ zlib"),
    ("GitHub Release 附件列表抓不到怎么办", ("mem", 484), "附件列表前端懒加载"),
    ("空 HOME 冒烟要放毒配置还是空目录", ("mem", 444), "毒配置才抓得住静默采信"),
    ("两个 PyInstaller 产物怎么比较依赖多少", ("mem", 486), "目录条目干扰 namelist"),
    ("qfluentwidgets 的 InfoBar 中文被截断", ("mem", 459), "InfoBar 宽度按字符估"),
    ("简报钩子装了但没生效怎么查", ("mem", 492), "独立脚本调 journal 要设 SINK"),
    ("工作台每组只显示二十条没有出口", ("rec", 3636), "分页出口是记录不是记忆"),
    ("大量删除被沙箱拦住提示 SAFE_DELETE_BULK", ("mem", 0), "新增样本：待积累结论"),
]


def _ids_of(out: dict, kind: str) -> list:
    return list(out.get("memory_ids") if kind == "mem" else out.get("record_ids") or [])


def run(verbose: bool = False, root: str = "", cases=None) -> int:
    root = root or str(_brain_root())
    cases = cases if cases is not None else CASES
    state = Path(tempfile.mkdtemp(prefix="recall_eval_")) / "state.json"
    hit = scored = 0
    misses = []
    for i, (prompt, want, why) in enumerate(cases, 1):
        kind, want_id = want
        try:
            out = brain.prompt_recall(root, prompt, agent="workbuddy",
                                      cwd=str(ROOT_DIR), session=f"eval-{i}",
                                      state_path=str(state), update_state=False)
        except Exception as e:  # noqa: BLE001 量具不能因为单条炸掉而中断
            print(f"[{i:02d}] ERROR {prompt!r}: {e}")
            continue
        ids = _ids_of(out, kind)
        if want_id == 0:
            if verbose:
                print(f"[{i:02d}] SKIP(无标注) top3={ids} terms={out.get('terms')}")
            continue
        scored += 1
        ok = want_id in ids[:3]
        hit += 1 if ok else 0
        if not ok:
            misses.append((prompt, kind, want_id, why, ids, out.get("terms")))
        if verbose:
            mark = "✓" if ok else "✗"
            print(f"[{i:02d}] {mark} want={kind}#{want_id} top3={ids} "
                  f"terms={out.get('terms')}")
    rate = (hit / scored * 100) if scored else 0.0
    print(f"\nRecall@3 = {hit}/{scored} = {rate:.1f}%")
    for prompt, kind, want_id, why, ids, terms in misses:
        print(f"  漏：{prompt}\n      期望 {kind}#{want_id}（{why}）实际 {ids} · terms={terms}")
    return 0 if rate >= 80.0 else 1


def _brain_root() -> str:
    env = os.environ.get("AGENTHUB_ROOT")
    if env:
        return env
    cfg = Path.home() / ".agenthub" / "config.json"
    try:
        import json
        return str(json.loads(cfg.read_text(encoding="utf-8")).get("root") or "")
    except (OSError, ValueError):
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--root", default="")
    args = ap.parse_args()
    return run(args.verbose, args.root)


if __name__ == "__main__":
    raise SystemExit(main())
