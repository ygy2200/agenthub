# -*- coding: utf-8 -*-
"""R1084：记录路查询串形态 A/B（生产代码切常量，不是离屏模拟器）。

R1082 基线：记录路 Recall@2 = 11/22 = 50.0%；R1083 归因：11 条失败里
NOT_IN_POOL 5 / HIT_GATE 3 / RARE_GATE 2 / CAP_OR_RANK 1 / **SEM_KILLED 0**。
「语义路送进来又被词法闸删」的假设被证伪；真瓶颈是 `query` 由
extract_prompt_terms 抽出的题面碎片组成，LIKE/BM25/语义/图四路一起拿不到候选。

单变量：只切 brain.RECALL_REC_QUERY_MODE（默认 off，关档逐位零回归）。
  off    现状，query = " ".join(terms)
  raw    直接送用户原句（BM25 与语义第三路拿到完整提问，而非碎片）
  mix    原句 + 抽词（保住 LIKE 路的词面命中）
  whole  只送权重 2 的整词，丢掉 bigram 碎片

判据沿用 R1074 预注册口径：Δrate>0 且 Δ噪声≤0 且 Δ耗时≤50ms。
分母由 off 档的 leak 判定锁定（R1082 实测 0 条泄漏，分母 22）。
外部依据：查询预处理与召回一致性（训练/服务同构）
  https://datarekha.com/interview/nlp-llm/stop-words-in-nlp
  中文 query 的「口语 → 可检索表达」改写
  https://m.hr-soft.cn/blogkeji/2026062447198.html

用法：
    D:/python311/python.exe tools/records_query_ab_r1084.py --out ../output/xxx.txt
"""
import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import _ab_common as ab  # noqa: E402
import records_recall_eval as rce  # noqa: E402

CONFIGS = [(m, m) for m in ("off", "raw", "mix", "whole")]
_ORIG = brain.RECALL_REC_QUERY_MODE


def apply(mode):
    brain.RECALL_REC_QUERY_MODE = mode


def restore():
    brain.RECALL_REC_QUERY_MODE = _ORIG


def query_shape_report():
    out = ["附：四档查询串形状对照（以 R1083 的 3 条 NOT_IN_POOL 样本为例）\n"]
    probes = ("手机上不走线也能 adb 调试吗，当时找了哪些做法",
              "手机加规则失败那次现场是怎么恢复干净的",
              "那个帧生成解锁工具最近到底有没有出新版本")
    for p in probes:
        terms = brain.extract_prompt_terms(p)
        q = " ".join(t for t, _ in terms)
        shapes = []
        for mode in ("off", "raw", "mix", "whole"):
            apply(mode)
            shapes.append(f"{mode}={brain._recall_record_query(p, q)!r}")
        restore()
        out.append(f"  {p!r}\n    " + "\n    ".join(shapes) + "\n")
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    try:
        ab.run_ab(label="R1084 记录路查询串 A/B（Recall@2）", configs=CONFIGS,
                  apply=apply, restore=restore, root=args.root, out=args.out,
                  cases=rce.CASES_REC, judge=ab.records_judge,
                  metric="Recall@2", header=query_shape_report())
    finally:
        restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
