# -*- coding: utf-8 -*-
"""B092 / R886 → R887：取代声明未裁决的只读点名 CLI（薄包装）。

R887 起核心逻辑收编进 `ops.supersede_declaration_audit`：桌面版 exe 只打包主程序
+ bundle，`tools/` 不在包里；体检若来 import 本脚本，会在桌面版静默失明。
本文件只保留命令行出口与报告排版，**不再有第二份实现**（单一事实源在 ops.py）。

背景与判据见 output/迭代依据-外部取证与取代声明断层-2026-11-05.md：
真库 559 条里 30 条正文写「取代记忆#N」，13 个目标仍 live；其中混着
局部取代 / 补充而非取代 / 续写仍有效，自动回填会误伤近一半 —— 所以只读点名，
绝不写库、绝不自动裁决。

用法：
  python tools/supersede_declaration_audit.py --db  D:/agent资料/zcode项目记录/_hub/brain.db
  python tools/supersede_declaration_audit.py --root D:/agent资料/zcode项目记录
  python tools/supersede_declaration_audit.py --db <db> --json
每次体检也已自动带上这项（ops.stale_health），不必再手工跑。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _console  # noqa: E402,F401  R222：会 print 的独立脚本必须先装 UTF-8 守卫
import ops  # noqa: E402

# R886 的 15 条用例按 audit(db_path) 取证；保留同名入口，避免两处实现漂移。
audit = ops.supersede_declaration_audit


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="只读点名「取代记忆#N」未裁决声明")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--db", type=Path, help="直接给 brain.db 路径")
    g.add_argument("--root", type=Path, help="给大脑数据根（含 _hub/brain.db）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = audit(str(a.db or a.root))
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
