# -*- coding: utf-8 -*-
"""R855 配套：把 `机制说明` 这枚标记回填到**历史**记忆上（一次性、需点名）。

为什么需要它：R855 判据是「写入口自己声明」——tags 里带 `机制说明` 的记忆，报错召回
不当修法用。但真库里那批老记忆（#509 精度闸门、#511 注入验收、#527 账本来历…）
是在标记存在之前写的，正文里引用着 `winerror 5` 这类招牌词，于是继续被当成「上次的修法」
注上去。机制上线了、库里一条都没标 = 修了个寂寞（R855 收尾实测：`error_recall` 仍把
#509 注到第一条）。

为什么不自动判：正文里「拿 WinError 5 当反例」和「WinError 5 的修法是」在文本上分不开，
猜错了会把真修法挡在门外。所以本工具**只做历史回填**（记忆 #527 的判据：拿名字猜只能用于
一次性回填，且清单要先收窄），两个限制：
  ① 不给正文打分，只按 `--ids` 点名；`--scan` 只是把候选列出来给人看，绝不自动选中。
  ② 默认预演，`--apply` 才写；每写一条留审计流水「记忆标签」+ 回滚方式。

用法：
    python tools/selfdoc_backfill.py --scan                 # 列候选（不改库）
    python tools/selfdoc_backfill.py --ids 509,511 --apply  # 点名回填
    python tools/selfdoc_backfill.py --ids 509 --apply --root D:\\path\\to\\hub
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _console  # noqa: E402,F401  控制台 UTF-8 守卫（Windows GBK 管道下 print 中文会崩）
import brain  # noqa: E402
import core  # noqa: E402

TAG = getattr(brain, "SELF_DOC_TAG", "机制说明")

# 候选只做「可能属于机制说明」的粗筛，命中不代表该标——列出来给人点名用。
SCAN_SQL = ("SELECT id, kind, tags, substr(content, 1, 70) AS head FROM memories "
            "WHERE kind IN ('lesson', 'note') "
            "AND (content LIKE '%设计口径%' OR content LIKE '%机制%' "
            "     OR content LIKE '%闸门%' OR content LIKE '%口径%') "
            "ORDER BY id DESC LIMIT 40")


def _tags_of(row) -> list[str]:
    return [t for t in str(row["tags"] or "").replace("，", ",").split(",") if t.strip()]


def scan(root: str) -> int:
    with brain.db_conn(root) as conn:
        rows = list(conn.execute(SCAN_SQL))
    if not rows:
        print("没有候选。")
        return 0
    print(f"候选 {len(rows)} 条（粗筛，不是判定；确认后用 --ids 点名）")
    for r in rows:
        mark = "✓已标" if TAG.lower() in {t.strip().lower() for t in _tags_of(r)} else "  "
        print(f"  {mark} #{r['id']} [{r['kind']}] {r['head'].replace(chr(10), ' ')}")
    return 0


def backfill(root: str, ids: list[int], apply: bool) -> int:
    if not ids:
        print("没有给 --ids；回填必须点名（本工具不按正文猜）。")
        return 2
    with brain.db_conn(root) as conn:
        targets = []
        for mid in ids:
            row = conn.execute("SELECT id, tags, kind, substr(content,1,60) AS head "
                               "FROM memories WHERE id=?", (mid,)).fetchone()
            if not row:
                print(f"  ! #{mid} 不存在，跳过")
                continue
            tags = _tags_of(row)
            if TAG.lower() in {t.strip().lower() for t in tags}:
                print(f"  = #{mid} 已带 {TAG}，跳过")
                continue
            targets.append((row, tags))
        if not targets:
            print("没有需要回填的行。")
            return 0
        print(f"{'写入' if apply else '预演'}：{len(targets)} 条将追加标签 {TAG}")
        for row, tags in targets:
            new_tags = brain._norm_tags(",".join(tags + [TAG]))
            print(f"  {'✎' if apply else '·'} #{row['id']} {row['tags'] or '(空)'} -> {new_tags}"
                  f"   | {row['head'].replace(chr(10), ' ')}")
            if not apply:
                continue
            fixed = brain._register_tags(conn, new_tags)
            conn.execute("UPDATE memories SET tags=? WHERE id=?", (fixed, row["id"]))
            brain._journal_row(conn, "workbuddy", "记忆标签",
                               f"memories#{row['id']}",
                               f"回填 {TAG}（R855）：改前 tags={row['tags'] or '(空)'}；"
                               f"回滚=把 tags 改回原值")
    if apply:
        print("完成。复验：python -c \"import brain;print(brain.error_recall(r'<root>', "
              "'PermissionError: [WinError 5]', count=False)['memory_ids'])\"")
    else:
        print("预演结束，未改库。加 --apply 写入。")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="把 机制说明 标记回填到历史记忆（默认预演）")
    ap.add_argument("--root", default="", help="大脑根目录（缺省读 ~/.agenthub/config.json）")
    ap.add_argument("--scan", action="store_true", help="列候选，不改库")
    ap.add_argument("--ids", default="", help="点名回填的记忆 id，逗号分隔")
    ap.add_argument("--apply", action="store_true", help="真写（缺省只预演）")
    args = ap.parse_args(argv[1:])

    root = args.root or core.get_root()
    if not root or not Path(root, "_hub", "brain.db").exists():
        print(f"根目录无效或没有库：{root}")
        return 2
    if args.scan:
        return scan(root)
    ids = [int(x) for x in args.ids.replace("，", ",").split(",") if x.strip().isdigit()]
    return backfill(root, ids, args.apply)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
