# -*- coding: utf-8 -*-
"""B092 / R886：正文「取代记忆#N」声明未裁决的**只读**点名量具。

背景（R885 真库只读探针）：本仓早有结构化取代机制
（memories.superseded_by / hub_memory_write(supersedes=) / hub_memory_supersede /
hub_consolidate；ops.stale_health 还把「已标记旧事实不得被检索返回」列为硬不变量），
但引导块要求写记忆的人在**正文**里标「取代记忆#N」——那是文本约定，不是工具调用。
真库 559 条里 30 条这么写，28 个目标中 13 个至今 active 且 superseded_by=0：
新旧会一起被检索/推送，而 stale_health 管不到（它只查已标记条目）。

为什么不自动回填：13 条里至少 6 条是局部/补充/续写/否定声明
（#13 取代 #12「中错误归因」、#36 取代 #34「第③条」、#93 取代 #92「的修法部分」、
 #445「部分取代」、#70「其内容仍有效，本条续写进展」、#400「补充而非取代记忆#252」）。
纯文本正则连否定句都分不出，自动裁决会误伤近一半。

依据（详见 output/迭代依据-外部取证与取代声明断层-2026-11-05.md）：
  graymatter ADR-007 —— 墓碑制，Recall 打分前过滤，Put 热路径不做自动冲突检测；
  OpenViking #5267   —— 证据不足标 disputed，不静默选一个；
  ClawBrain #17      —— 部分取代是开放设计问题，衰减不是显式更正的替代品。

所以本工具只读：点名 + 启发式分类 + 给人工裁决入口，**不写库、不自动取代**。
分类是提示不是裁决；「未见限定词」也不等于「该取代成立」。

用法：
  python tools/supersede_declaration_audit.py --db D:/agent资料/zcode项目记录/_hub/brain.db
  python tools/supersede_declaration_audit.py --db <db> --json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

DECL_RE = re.compile(r"取代(?:记忆)?\s*#?\s*(\d+)")

# 命中任一即表示「不是整条取代」，只能提示人工判定。顺序＝优先级（先否定、后续写、再局部）。
QUALIFIER_RULES = (
    ("补充而非取代", ("补充而非取代", "而非取代", "补充而非")),
    ("续写仍有效", ("仍有效", "续写", "内容仍")),
    ("局部取代", ("部分取代", "的修法部分", "的修法", "第③条", "第②条", "第①条", "部分")),
)
CLASSES = ("未见限定词", "局部取代", "续写仍有效", "补充而非取代")
RETIRED = ("retired", "expired", "superseded")

# R886 真库实跑抓到的自指污染（B040 同族）：讲这套机制本身的记忆里会引用
# 「#13 取代 #12…」当例句，正则照样命中，凭空多出 4 条假声明（13→19）。
# 优先用结构化标签排（B040 口径），正文自指词兜底（写的人漏标时也不失真）。
SELF_DOC_TAGS = ("机制说明",)
SELF_DOC_BODY = ("取代声明 ≠", "取代声明审计", "B092", "本工具只读", "外部取证", "量具")


def _is_selfdoc(row: dict) -> bool:
    tags = str(row.get("tags") or "")
    body = str(row.get("content") or "")
    return any(t in tags for t in SELF_DOC_TAGS) or any(m in body for m in SELF_DOC_BODY)


def classify(text: str) -> str:
    """启发式分类：只给提示，不是裁决。无命中＝「未见限定词」，仍需人工确认。"""
    for label, words in QUALIFIER_RULES:
        if any(w in text for w in words):
            return label
    return "未见限定词"


def audit(db: Path) -> dict:
    """只读打开真库，返回待判定/已裁决/目标不存在三组清单。"""
    uri = "file:" + str(db).replace("\\", "/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = {r["id"]: dict(r) for r in con.execute("SELECT * FROM memories")}
    finally:
        con.close()

    declarations, skipped = [], []
    for mid, row in rows.items():
        body = str(row.get("content") or "")
        ids = DECL_RE.findall(body)
        if not ids:
            continue
        if _is_selfdoc(row):
            skipped.append(mid)
            continue
        declarations.append((mid, ids, body))

    pending, resolved, dangling = [], [], []
    seen = set()
    for mid, ids, body in declarations:
        cls = classify(body)
        for raw in ids:
            tid = int(raw)
            if tid == mid or (mid, tid) in seen:
                continue
            seen.add((mid, tid))
            target = rows.get(tid)
            if target is None:
                dangling.append({"decl": mid, "target": tid, "class": cls})
                continue
            sup = int(target.get("superseded_by") or 0)
            live = sup == 0 and (target.get("status") or "active") not in RETIRED
            item = {"decl": mid, "target": tid, "class": cls,
                    "target_status": target.get("status"),
                    "target_superseded_by": sup}
            (pending if live else resolved).append(item)

    return {
        "total_memories": len(rows),
        "declarations": len(declarations),
        "distinct_pairs": len(seen),
        "pending": pending,
        "resolved": resolved,
        "dangling": dangling,
        "skipped_selfdoc": skipped,
        "distinct_live_targets": sorted({x["target"] for x in pending}),
        "by_class": {c: sum(1 for x in pending if x["class"] == c) for c in CLASSES},
        "note": "启发式分类不是裁决；pending 是「声明对」，distinct_live_targets 才是被点名的旧记忆条数。",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="只读点名「取代记忆#N」未裁决声明")
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = audit(a.db)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print("取代声明审计（只读，不写库）")
    print(f"  记忆 {r['total_memories']} 条 / 含声明 {r['declarations']} 条 / 声明对 {r['distinct_pairs']} 组")
    print(f"  自述/机制说明已排除 {len(r['skipped_selfdoc'])} 条 {r['skipped_selfdoc']}")
    print(f"  待人工判定 {len(r['pending'])} 组（涉及 {len(r['distinct_live_targets'])} 条旧记忆）"
          f"  已裁决 {len(r['resolved'])} 组  目标不存在 {len(r['dangling'])} 组")
    print(f"  待判定分类：{r['by_class']}")
    for x in r["pending"]:
        print(f"    #{x['decl']} -> #{x['target']}  [{x['class']}]  status={x['target_status']}")
    if r["dangling"]:
        print("  目标不存在（不猜、不建）：", r["dangling"])
    print("  裁决入口：hub_memory_write(supersedes=旧id) 或 hub_memory_supersede(keep_id, drop_id)")
    print("  " + r["note"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
