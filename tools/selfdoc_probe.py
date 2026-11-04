# -*- coding: utf-8 -*-
"""R855 量具：报错召回里，哪些记忆是「跨签名通吃」的。

判据：一条**真修法**只对该签名的错有用；一条**机制说明**会把招牌词当例子引用，
于是对一堆互不相关的错都命中。本工具拿一组互不相关的真实报错各跑一次
`brain.error_recall`，把「出现在 ≥2 个签名里的记忆」点名——这些就是自我引用污染的
头号嫌疑（不是定罪：跨签名也可能因为它同时记了两个坑，看正文自己判）。

    python tools/selfdoc_probe.py            # 默认根目录 + 内置 7 条
    python tools/selfdoc_probe.py --json     # 给 CI/台账引用
    python tools/selfdoc_probe.py --from-db 20   # 再从真库 errors 里取 N 条真实报错

为什么不在测试里跑真库：单测用种子库（`test_error_selfdoc_r855.py` 钉机制），
本工具是**真库观测**，两者分工不同，别拿它当门禁用。

用例数决定「跨签名通吃」这条判据的**统计功效**：只有 7 个签名时，一条通吃的说明书
大概率只撞进 1 个，报「无」是量具太窄不是库干净（R855 第一轮实测正是这样——#509 明明白白
注在 winerror5 上，却因为只出现一次而没被点名）。`--from-db` 把用例铺到 20+ 条真实报错，
同一条说明书跨签名的次数才数得出来。
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _console  # noqa: E402,F401  控制台 UTF-8 守卫（Windows GBK 管道下 print 中文会崩）
import brain  # noqa: E402
import core  # noqa: E402

CASES = [
    ("winerror5", "PermissionError: [WinError 5] Access is denied: 'C:\\\\Users\\\\y\\\\.agenthub\\\\config.json'"),
    ("modnotfound", "ModuleNotFoundError: No module named 'PySide6.QtPrintSupport'"),
    ("dblocked", "sqlite3.OperationalError: database is locked"),
    ("jsondecode", "json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)"),
    ("noneattr", "AttributeError: 'NoneType' object has no attribute 'get'"),
    ("gitpush", "fatal: unable to access 'https://github.com/': Failed to connect to github.com port 443"),
    ("encoding", "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd6 in position 0: invalid continuation byte"),
]


def _db_cases(root: str, want: int) -> list:
    """从真库 errors 表取真实报错原文当用例（detail 优先，正文里常有原始报错）。

    两道收窄：① 按**签名**去重——同一条报错登记 7 次会让「通吃」计数虚高，判据数的是
    互不相干的签名，不是用例条数；② 大脑自己的工具（hub_*/agenthub）不算——那条路
    `error_recall` 的 self_tool 闸门本来就拦，留在用例里只会稀释分母。
    """
    out, seen = [], set()
    try:
        con = sqlite3.connect(f"file:{brain.db_path(root)}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT id, title, detail FROM errors ORDER BY id DESC").fetchall()
    except sqlite3.Error:
        return out
    try:
        for r in rows:
            text = (r["detail"] or r["title"] or "").strip()
            if len(text) < 12:
                continue
            low = text.lower()
            if "hub_" in low or "mcp__agenthub__" in low or "agenthub" in low:
                continue
            terms = brain.error_signature_terms(text)
            if not terms or not any(w >= 3 for _, w in terms):
                continue                      # 抽不出招牌词的报错，召回本来就不该开口
            key = "|".join(t for t, _ in terms)
            if key in seen:
                continue
            seen.add(key)
            out.append((f"err{r['id']}", text[:400]))
            if len(out) >= want:
                break
    finally:
        con.close()
    return out


def run(root: str, cases: list) -> dict:
    per_case, hits = {}, collections.Counter()
    for name, text in cases:
        out = brain.error_recall(root, text, count=False)
        per_case[name] = {"memory_ids": out["memory_ids"],
                          "self_doc_ids": out.get("self_doc_ids", [])}
        for mid in out["memory_ids"]:
            hits[mid] += 1
    return {"cases": per_case,
            "n_cases": len(cases),
            "repeat": [{"id": mid, "cases": n} for mid, n in hits.most_common() if n >= 2]}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="报错召回跨签名通吃体检（真库，只读）")
    ap.add_argument("--root", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--from-db", type=int, default=0, metavar="N",
                    help="额外从真库 errors 取 N 条真实报错（按签名去重）当用例")
    args = ap.parse_args(argv[1:])
    root = args.root or core.get_root()
    if not root or not Path(root, "_hub", "brain.db").exists():
        print(f"根目录无效或没有库：{root}")
        return 2
    cases = list(CASES) + _db_cases(root, max(0, args.from_db))
    data = run(root, cases)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    print(f"报错召回跨签名体检（{data['n_cases']} 个互不相关的真实报错）")
    for name, v in data["cases"].items():
        print(f"  {name:12} 注入 {v['memory_ids']}"
              + (f" · 机制说明已拦 {v['self_doc_ids']}" if v["self_doc_ids"] else ""))
    if not data["repeat"]:
        print(f"跨签名通吃：无（{data['n_cases']} 个签名里，没有记忆出现在 2 个以上）")
    else:
        print(f"跨签名通吃（≥2 个签名命中，优先怀疑自我引用；分母={data['n_cases']} 个签名）：")
        for r in data["repeat"]:
            row = brain.memory_get(root, r["id"]) if hasattr(brain, "memory_get") else None
            head = ""
            if isinstance(row, dict):
                head = str(row.get("content", ""))[:60].replace("\n", " ")
            print(f"  #{r['id']} ×{r['cases']} 个签名  {head}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
