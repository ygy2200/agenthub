# -*- coding: utf-8 -*-
"""R861 修账：把「钩子探针」打出来的曝光计数还原，别让自家测试把好记忆判成免疫。

背景：R830 的曝光免疫按 inject_count>=5 且 30 天内零采纳冷却一条记忆。R861 的钩子
时延探针直接跑真实钩子（origin=live），同一批记忆被连续推 5 次、全部零采纳 →
#353/#457/#487 等被判「推了没用」。产品侧已加 origin 闸门（eval/probe 不再计曝光），
本脚本只修历史账。目标行必须**同时**满足：
  ① inject_count == IMMUNE_INJECT_MIN（刚摸到免疫线，不是长期累积）
  ② adopt_count == 0
  ③ last_inject 落在 --since 之后（默认 2026-11-04T14:30，本机探针窗口起点）
满足的行把 inject_count 归零、last_inject 清空，写 journal（曝光修复）并留 DB 备份。
默认只看不写，加 --yes 才落库。--ids 可限定只修明确点名的行。

用法：
  python tools/exposure_probe_pollution_purge_r861.py --root "D:\\agent资料\\zcode项目记录"
  python tools/exposure_probe_pollution_purge_r861.py --root ... --ids 353,457 --yes
"""
import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import core  # noqa: E402


def _q(conn, sql, args=()):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, args)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="", help="项目根（缺省取 AgentHub 配置）")
    ap.add_argument("--since", default="2026-11-04T14:30")
    ap.add_argument("--ids", default="", help="逗号分隔，限定只修这些 id")
    ap.add_argument("--yes", action="store_true", help="真正落库（默认只读预览）")
    args = ap.parse_args()

    root = args.root or core.get_root()
    db = Path(root) / core.DIR_META / "brain.db"
    if not db.is_file():
        print(f"[X] 找不到 {db}")
        return 2
    conn = sqlite3.connect(str(db))
    try:
        sql = ("SELECT id, project, inject_count, adopt_count, last_inject, "
               "substr(replace(content, char(10), ' '), 1, 46) AS head "
               "FROM memories WHERE inject_count=? AND adopt_count=0 "
               "AND COALESCE(last_inject,'')>=? AND IFNULL(superseded_by,0)=0 "
               "AND (status IS NULL OR status NOT IN ('deleted','rejected'))")
        rows = _q(conn, sql + " ORDER BY id", (brain.IMMUNE_INJECT_MIN, args.since))
        if args.ids:
            keep = {int(x) for x in args.ids.replace("，", ",").split(",") if x.strip()}
            rows = [r for r in rows if r["id"] in keep]
        if not rows:
            print(f"[OK] 没有命中：{args.since} 之后没有「inject=={brain.IMMUNE_INJECT_MIN}"
                  f" 且零采纳」的行")
            return 0
        print(f"候选 {len(rows)} 行（since={args.since}，inject=={brain.IMMUNE_INJECT_MIN}，adopt=0）：")
        for r in rows:
            print(f"  mem#{r['id']:<5} {r['project'] or '（全局）':<22} "
                  f"inject={r['inject_count']} last={r['last_inject']}  {r['head']}")
        if not args.yes:
            print("\n[只读预览] 加 --yes 才会归零并写审计")
            return 0

        bak_dir = Path(root) / core.DIR_META / "brain-backups"
        bak_dir.mkdir(parents=True, exist_ok=True)
        bak = bak_dir / (f"brain-{datetime.datetime.now():%Y%m%d-%H%M%S}"
                         "-pre-exposure-reset.db")
        dst = sqlite3.connect(str(bak))
        with dst:
            conn.backup(dst)
        dst.close()
        print(f"[备份] {bak}  {bak.stat().st_size} B")

        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE memories SET inject_count=0, last_inject='' WHERE id IN ({marks})", ids)
        conn.commit()
        brain.journal_add(root, "qoder", "曝光修复", f"memories#{len(ids)}",
                          backup=str(bak),
                          note=("探针污染归零（R861）：inject==5 且零采纳、last_inject>=%s；"
                                "产品侧已加 origin 闸门。ids=%s" % (args.since, ids)),
                          via="tools")
        after = _q(conn, f"SELECT id, inject_count, last_inject FROM memories "
                         f"WHERE id IN ({marks})", ids)
        print("[完成] " + ", ".join(f"#{r['id']} inject={r['inject_count']}" for r in after))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
