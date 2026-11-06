# -*- coding: utf-8 -*-
"""R937 / B088 只读量具：空指纹(fp=[])注入行到底是不是缺陷。

口径（大脑记忆 #581，必须遵守）：先按 origin 把生产行(live/brief)与装置行
(eval/probe)分开报，再拿总数谈优先级。拿 144 行总数去改抽取器 = 拿装置噪声当需求。

诊断两问：
  1. 这 144 行里有多少是真实会话产生的？
  2. 抽不出指纹，是「记忆本身没有任何招牌 token」(结构性不可判)，
     还是「有 token 但被过滤/抽取器漏了」(可修)？

只读：sqlite mode=ro，不写任何数据。运行：
  D:/python311/python.exe tools/adherence_unobservable_r937.py --out <path.json>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import _console  # noqa: E402,F401  R222 结构性门禁：会 print 的脚本必须装 UTF-8 守卫
import brain  # noqa: E402

DB = Path(r"D:\agent资料\zcode项目记录\_hub\brain.db")
EMPTY = "fp is null or trim(fp) in ('','[]')"
PRODUCTION = ("live", "brief")
DEVICE = ("eval", "probe")


def _raw_tokens(text: str) -> list:
    """正则形状命中了几个（过滤前）。"""
    out = []
    for pat in brain._FP_PATTERNS:
        for m in pat.finditer(text or ""):
            out.append(brain._FP_CUT.split(m.group(0))[0].strip().rstrip(".,;:)）】」").lower())
    return out


def diagnose(content: str, tags: str) -> str:
    """两类硬理由，不猜第三类。"""
    text = (content or "") + " " + (tags or "")
    raw = _raw_tokens(text)
    if not raw:
        return "no_tokens"          # 结构性：纯自然语言行为规则，没有招牌形态
    return "has_tokens_but_filtered"  # 有形状却被 idf/长度/提问覆盖滤掉 -> 可查


def run(db_path: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    by_origin = dict(conn.execute(
        f"select coalesce(nullif(origin,''),'(blank)') o, count(*) n "
        f"from recall_adherence where {EMPTY} group by 1").fetchall())
    totals = dict(conn.execute(
        "select coalesce(nullif(origin,''),'(blank)') o, count(*) n "
        "from recall_adherence group by 1").fetchall())

    prod_rows = [dict(r) for r in conn.execute(
        f"select id,ts,agent,memory_id,strength,origin,directive from recall_adherence "
        f"where {EMPTY} and origin in ({','.join('?' * len(PRODUCTION))}) "
        f"order by id", PRODUCTION)]

    by_memory = Counter(r["memory_id"] for r in prod_rows)
    detail, reasons = [], Counter()
    for r in prod_rows:
        m = conn.execute("select content,tags,status from memories where id=?",
                         (r["memory_id"],)).fetchone()
        content = (m["content"] if m else "") or ""
        tags = (m["tags"] if m else "") or ""
        why = diagnose(content, tags)
        reasons[why] += 1
        detail.append({**r, "reason": why,
                       "memory_exists": bool(m),
                       "content_head": content[:90]})

    out = {
        "purpose": "R937/B088 空指纹注入行按来历分段 + 不可判成因诊断（只读）",
        "db": str(db_path),
        "empty_fp_total": sum(by_origin.values()),
        "empty_by_origin": by_origin,
        "all_rows_by_origin": totals,
        "production_rows": len(prod_rows),
        "device_rows": sum(n for o, n in by_origin.items() if o in DEVICE),
        "production_rate": round(len(prod_rows) /
                                 max(1, sum(n for o, n in totals.items()
                                            if o in PRODUCTION)), 4),
        "by_memory_top": by_memory.most_common(10),
        "reason_counts": dict(reasons),
        "detail": detail,
        "verdict": ("待判定" if not prod_rows else
                    "结构性不可判(no_tokens) 占多数 -> B088 的『补抽取』方向不成立"
                    if reasons["no_tokens"] > reasons["has_tokens_but_filtered"] else
                    "存在可修抽取缺口 -> 值得补抽取"),
    }
    conn.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    res = run(Path(a.db))
    text = json.dumps(res, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(f"空 fp 总数 {res['empty_fp_total']} | 生产 {res['production_rows']} "
          f"| 装置 {res['device_rows']} | 生产占比 {res['production_rate']:.2%}")
    print(f"按 origin：{res['empty_by_origin']}")
    print(f"成因：{res['reason_counts']}")
    print(f"结论：{res['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
