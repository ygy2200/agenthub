# -*- coding: utf-8 -*-
"""R1085：修完标注噪声后，复评 R1084 四档查询串（同一量具、同一判据）。

R1084 的 raw 档 +18.2pp / 零转丢失，却因 Δ噪声 +6 被判 KEEP-OFF。
关键问题：那 6 格到底是不是「答对了另一条」？R1085 逐条读库判读 19 个非目标格：
  · 只有 2 条是同一件事的另一条合法记录 —— #3689（结晶 #14，对应 #3688 待办 #14）、
    #3687（待办 #7，对应 #3691 结晶 #7，反向同理）。
  · 其余 17 条是跨项目真噪声（Wireshark 安装 / CS2 卡死 / 课表 App / 联想黑屏…）。
按 R891 的口径把前两条补进合格集（不是放水：只补能指认同任务的，
且 off 与 raw 两档同样受益），再复评四档。

预注册判据（与 R1084 完全一致，不许事后改）：Δrate>0 且 Δ噪声≤0 且 Δ耗时≤50ms。
若 raw 仍不满足 Δ噪声≤0，则结论写死为「增益真、噪声也真」，默认档保持 off，
下一步转「精度闸门」方向，不许靠改判据让它过关。

用法：
    D:/python311/python.exe tools/records_query_recheck_r1085.py --out ../output/xxx.txt
"""
import argparse
import io
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import _ab_common as ab  # noqa: E402
import records_query_ab_r1084 as rq  # noqa: E402
import records_recall_eval as rce  # noqa: E402


def label_report():
    buf = io.StringIO()
    buf.write("附：R1085 合格集补标明细（只补能指认同一任务的）\n")
    for i, (p, t, w) in enumerate(rce.CASES_REC, 1):
        acc = sorted(rce.accepts_of(t))
        if len(acc) > 1:
            buf.write(f"  [{i:02d}] {p!r} → {acc}  # {w}\n")
    buf.write("  判读结论：19 个非目标格里 2 条合法（#3689 / #3687），17 条为真噪声。\n")
    buf.write("  未补的候选：#3725（重启 qoder，问题问的是截图那次）、"
              "#3721（只读调研，问题问的是编译器实编译）、其余均跨项目无关。\n")
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    try:
        ab.run_ab(label="R1085 记录路查询串复评（标注修正后，Recall@2）",
                  configs=rq.CONFIGS, apply=rq.apply, restore=rq.restore,
                  root=args.root, out=args.out, cases=rce.CASES_REC,
                  judge=ab.records_judge, metric="Recall@2",
                  header=label_report() + "\n" + rq.query_shape_report())
    finally:
        rq.restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
