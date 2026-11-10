# -*- coding: utf-8 -*-
"""R1098：ISO 日期修复的同进程 A/B —— 零回归 + 修复生效，一次量完。

R1098 改了 `brain._parse_time_range`（生产代码），必须有严格零回归证据。
跨文件比对不合格：R908 规定 env/db 指纹不同的两轮不可直接比，而真库一直在长
（R1095 时 db=5a8dea882229，现在 6692f133702d）。所以本工具**同一进程、
同一真库快照、同一批 73 条样本**里切两档：
  old  把 ISO 日期串从提问里抹掉再调新解析器 = 模拟修复前行为
  new  当前实现（含 ISO 分支）
判据写在前：73 条上 Δrate=0、Δ噪声=0、逐样本 cls 零翻转才算零回归；
另用 6 条带 ISO 日期的定向提问，要求 new 档 time_window 点亮而 old 档不点亮。

用法：
    D:/python311/python.exe tools/iso_regression_ab_r1098.py --out ../output/xxx.txt
"""
import argparse
import io
import re
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import _abroot  # noqa: E402
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402
import _ab_common as ab  # noqa: E402

ISO_RE = re.compile(r"(?<!\d)(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)")
_NEW = brain._parse_time_range
TARGETS = (
    "2026-10-25 那次调研结论是什么",
    "查 2026/11/06 的工作记录",
    "2026.11.09 发版之后改了什么",
    "2026-1-5 那次复盘",
    "今天和 2026-10-01 之间做了什么",
    "没有日期的普通提问怎么修",
)


def _old(q):
    return _NEW(ISO_RE.sub("", q or ""))


def apply(mode):
    brain._parse_time_range = _old if mode == "old" else _NEW


def restore():
    brain._parse_time_range = _NEW


def targeted(root, out):
    state = Path(tempfile.mkdtemp(prefix="iso_ab_")) / "state.json"
    snap = _abroot.snapshot_root(root)
    try:
        out.write("二、ISO 定向提问（要求修复档点亮 time_window、旧档不点亮）\n")
        for p in TARGETS:
            cells = {}
            for mode in ("old", "new"):
                apply(mode)
                r = brain.prompt_recall(
                    snap, p, agent="workbuddy", cwd=str(ROOT_DIR),
                    session=f"iso-{mode}", state_path=str(state),
                    update_state=False, origin="probe")
                cells[mode] = (r.get("time_window"),
                               int(r.get("time_window_dropped") or 0))
            out.write(f"  {p!r}\n    old={cells['old']}  new={cells['new']}\n")
    finally:
        _abroot.drop_root(snap)
        restore()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = args.root or ev._brain_root()
    buf = io.StringIO()
    buf.write(f"R1098 ISO 修复同进程 A/B | 真库 {root} | 样本 {len(ev.CASES)} 条\n")
    buf.write(ev.env_guard_line(root) + "\n")
    buf.write("一、73 条评测集零回归（基准 = old 模拟修复前）\n")
    try:
        results = ab.run_ab(label="R1098 ISO 修复零回归 A/B（Recall@3）",
                            configs=[("old", "old"), ("new", "new")],
                            apply=apply, restore=restore, root=root,
                            cases=ev.CASES, verbose=False,
                            header="判据：Δrate=0、Δ噪声=0、逐样本零翻转才算零回归。\n")
    finally:
        restore()
    base, new = results[0], results[1]
    flips = [(i, a, b) for (i, a, _), (j, b, _) in
             zip(base["per_case"], new["per_case"]) if i == j and a != b]
    buf.write(f"  old  {base['hit']}/{base['scored']}={base['rate']}% 噪声={base['noise']}\n")
    buf.write(f"  new  {new['hit']}/{new['scored']}={new['rate']}% 噪声={new['noise']}\n")
    buf.write(f"  逐样本类别翻转：{len(flips)} 条 {flips[:10]}\n")
    ok = (base["rate"] == new["rate"] and base["noise"] == new["noise"]
          and not flips)
    buf.write(f"  零回归判定：{'PASS' if ok else 'FAIL'}\n\n")
    targeted(root, buf)
    text = buf.getvalue()
    print(text)
    if args.out:
        q = Path(args.out)
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text(text, encoding="utf-8")
        print(f"已写：{q}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
