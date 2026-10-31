# -*- coding: utf-8 -*-
"""R825：回忆质量的第二把量具 —— 项目近况块覆盖率。

为什么和 recall_eval 分开：`prompt_recall` 现在有两条腿，一条走记忆检索
（命中靠词），一条走「本项目近况」（命中靠 cwd 推断出的项目名）。Recall@3
只量第一条。这一把量第二条：在项目目录里问一句「继续迭代」，注入内容里到底
有没有「上一轮做到哪 / 还剩什么」。

用法：
    python tools/project_block_eval.py -v

口径：对每个真实项目目录问一句通用推进语，断言注入文本里出现
①【本项目近况】标题；②该项目最近一条工作记录的关键词；③未完成任务数行。
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

# (项目名, 该提问, 近况块里必须出现的线索)
# 线索取「记录行必然带的东西」：日期前缀 + 标题里最独特的一个词。别拿项目主题词
# （红色沙漠/帧生成）当线索——最近几条记录可能只谈操作细节，标题里根本不出现。
CASES = [
    ("AgentHub-多agent文件统一管理", "继续迭代，下一步做什么", "R8"),
    ("红色沙漠模组管理", "接着弄", "10-31"),
    ("按进程流量管控-调研", "继续", "键盘"),
    ("按进程流量管控-调研", "下一步", "键盘"),
]


def run(verbose: bool = False, root: str = "") -> int:
    root = root or _brain_root()
    state = Path(tempfile.mkdtemp(prefix="proj_eval_")) / "state.json"
    ok = 0
    for i, (project, prompt, kw) in enumerate(CASES, 1):
        cwd = str(Path(root) / project)
        out = brain.prompt_recall(root, prompt, agent="workbuddy", cwd=cwd,
                                  session=f"pe-{i}", state_path=str(state),
                                  update_state=False)
        text = out.get("text") or ""
        has_head = "本项目近况" in text
        has_kw = kw in text
        good = has_head and has_kw
        ok += 1 if good else 0
        if verbose or not good:
            mark = "✓" if good else "✗"
            print(f"[{i}] {mark} {project}: 标题={has_head} 关键词({kw})={has_kw} "
                  f"chars={len(text)}")
            if not good and verbose:
                print("    " + text.replace("\n", "\n    ")[:600])
    print(f"\n项目近况覆盖 = {ok}/{len(CASES)} = {ok / len(CASES) * 100:.0f}%")
    return 0 if ok == len(CASES) else 1


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
    a = ap.parse_args()
    return run(a.verbose, a.root)


if __name__ == "__main__":
    raise SystemExit(main())
