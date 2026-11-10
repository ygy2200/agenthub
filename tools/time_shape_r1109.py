#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R1109 · 时间限窗「形态 × 选择率」只读归因。

背景：R1108 把配对样本扩到 16 条后，on 4/16 → off 12/16、g=8/l=0、
McNemar p=0.0078（显著），但 Δ噪声 +10 卡住判据。要判断下一步该修哪里，
必须先回答：**是哪些时间词形态在误杀？**

假设（写在前的判据）：
  H1 相对式词（最近N天/上周/本周/昨天/今天）被当成「记录写入日期」的硬过滤，
     而记忆库里绝大多数答案的日期远早于该窗口 → 窗口把答案整批删掉。
  H2 绝对式词（ISO 日期 / X月）窗口较窄但语义更接近「那次」，误杀程度不同。
  判据：若某形态的「库内窗口选择率」中位数 < 5%，则该形态在 89 条语料上
  必然误杀，应进「形态判别」白名单/黑名单讨论；若 > 30%，则该形态本身无害。

口径说明（重要）：
  * 选择率 = 库内落在窗口内的 active 记忆数 ÷ active 记忆总数（记录路另算）。
    它是**语料无关**的客观指标，不依赖本次是否命中，可复跑。
  * 只读：不写库、不改常量、不产生 inject/use 账。

用法：python tools/time_shape_r1109.py [--out 证据.md]
"""
import argparse
import datetime
import io
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recall_eval as ev  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brain  # noqa: E402


SHAPES = {
    "单日相对": ("昨天", "今日", "今天", "昨日"),
    "周": ("上周", "本周", "这周", "上星期"),
    "最近N": ("最近", "近"),
    "本月": ("本月", "这个月"),
    "上月": ("上月", "上个月"),
    "ISO单日": ("iso",),
    "月中日": ("月", "号", "日"),
}


def classify(prompt, window):
    """按词面形态归类；顺序敏感（先判最具体的）。"""
    p = prompt or ""
    if window and window[0] == window[1] and len(window[0]) == 10:
        # 单日窗口：ISO 直写 或 昨天/今天
        if any(t in p for t in ("昨天", "昨日", "今天", "今日")):
            return "单日相对"
        return "ISO单日"
    if any(t in p for t in ("上周", "上星期", "本周", "这周")):
        return "周"
    if any(t in p for t in ("本月", "这个月")):
        return "本月"
    if "上个月" in p or "上月" in p:
        return "上月"
    if "最近" in p or "近" in p:
        return "最近N"
    if "月" in p:
        return "月中日"
    return "其他"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = ev._brain_root()
    db = str(brain.db_path(root))
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    def corpus(table, where_extra=""):
        # 列名先 pragma 再写 SQL（大脑记忆 #20 的坑）：memories 用 created、
        # records 用 date，两表不统一。写死 date 会 OperationalError。
        cols = [r[1] for r in
                conn.execute(f"PRAGMA table_info({table})")]
        col = "date" if "date" in cols else ("created" if "created" in cols else "")
        if not col:
            return []
        q = f"SELECT id, {col} AS d FROM {table} WHERE status='active'"
        if table == "memories":
            q += " AND (superseded_by IS NULL OR superseded_by=0)"
        if where_extra:
            q += " AND " + where_extra
        return [(int(r["id"]), str(r["d"] or "")[:10])
                for r in conn.execute(q)]

    mems = corpus("memories")
    recs = corpus("records")

    ids = [i for i, (_p, _w, why) in enumerate(ev.CASES, 1)
           if str(why).startswith("时间词配对")]
    buf = io.StringIO()
    buf.write("R1109 时间限窗形态 × 库内选择率（只读）\n")
    buf.write(f"语料：active 记忆 {len(mems)} 条 · active 记录 {len(recs)} 条 ·"
              f" 配对样本 {len(ids)} 条\n")
    buf.write("选择率 = 落在窗口内的 active 条数 ÷ 总数；<5% 视为该形态必然误杀\n\n")
    buf.write(f"{'#':<4}{'形态':<10}{'窗口':<26}{'宽':<5}"
              f"{'记忆选择率':<12}{'记录选择率':<12}提问\n")

    rows = []
    for i in ids:
        prompt = ev.CASES[i - 1][0]
        w = brain._parse_time_range(prompt)
        shape = classify(prompt, w)
        if not w:
            rows.append((i, shape, None, 0, 0.0, 0.0, prompt))
            buf.write(f"{i:<4}{shape:<10}{'(未解析)':<26}{0:<5}"
                      f"{'-':<12}{'-':<12}{prompt[:40]}\n")
            continue
        lo, hi = w
        width = (datetime.date.fromisoformat(hi)
                 - datetime.date.fromisoformat(lo)).days + 1
        m_in = sum(1 for _id, d in mems if d and lo <= d <= hi)
        r_in = sum(1 for _id, d in recs if d and lo <= d <= hi)
        mr = m_in / max(1, len(mems)) * 100
        rr = r_in / max(1, len(recs)) * 100
        rows.append((i, shape, w, width, mr, rr, prompt))
        buf.write(f"{i:<4}{shape:<10}{lo}~{hi:<12}{width:<5}"
                  f"{mr:>6.1f}% ({m_in:<4}){rr:>7.1f}% ({r_in:<3}){prompt[:38]}\n")

    buf.write("\n按形态汇总（选择率中位数）\n")
    by = {}
    for i, shape, w, width, mr, rr, _p in rows:
        by.setdefault(shape, []).append((mr, rr, width, i))
    for shape, vals in sorted(by.items(), key=lambda kv: min(v[0] for v in kv[1])):
        ms = sorted(v[0] for v in vals)
        n = len(ms)
        med = ms[n // 2] if n % 2 else (ms[n // 2 - 1] + ms[n // 2]) / 2
        med_w = sorted(v[2] for v in vals)[n // 2]
        flag = "必然误杀" if med < 5.0 else ("无害" if med > 30.0 else "部分误杀")
        buf.write(f"  {shape:<10} n={n:<3} 记忆选择率中位数={med:6.2f}%  "
                  f"窗口中位宽={med_w:<4}天  判定={flag}  样本={[v[3] for v in vals]}\n")

    conn.close()
    buf.write("\n判据（写在前的）：形态选择率中位数 <5% → 该形态在 89 条语料上必然误杀，"
              "应讨论形态判别；>30% → 形态本身无害，误杀来自别处。\n")
    text = buf.getvalue()
    print(text)
    if args.out:
        p = Path(args.out)
        p.write_text(text, encoding="utf-8")
        print(f"已写：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
