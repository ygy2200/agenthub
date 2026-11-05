# -*- coding: utf-8 -*-
"""R882：把用户级硬规则标成 tags=必做，供简报「共享规则簿」每次开工自动送达。

为什么只标这 8 条：真库 556 条记忆里，真正「以后遇到同类事必须照做」的用户规则
是少数；全量 preference（14 条）搬进规则段会退化成没人看的长背景。逐条判过：
  #76  自主推进/先学后做（协作方式）
  #98  不要在回复里贴闸门快照
  #205 FlClash 一律走技能，不裸命令行
  #236 skill/MCP/记忆先从 AgentHub 取
  #268 只做明确要求的步骤 + 结论先行
  #283 清理类任务不要自动加回收站兜底
  #366 不要天天问；执行层连串动作做完再报
  #408 发布按里程碑，不逐轮发 Release
未选：#90（被 #205 覆盖）、#175/#287（被 #288 取代）、#183/#184（N 卡专项，非每会话）、
#288（项目授权，不是通用硬约束）。

纪律：默认预演；--apply 才写；写前整库在线备份；每条改动进 journal（含旧 tags），
可按备份路径整体回滚。只改 tags，不碰 updated（否则旧规则会装成新记忆）。
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brain  # noqa: E402
import core  # noqa: E402
import _console  # noqa: E402,F401  控制台 UTF-8 守卫（R179）

core.JOURNAL_SINK = brain.journal_add

TARGETS = {
    76: "协作方式：自主推进/先学后做",
    98: "回复不要贴闸门快照",
    205: "FlClash 一律走技能",
    236: "资产先从 AgentHub 取",
    268: "只做明确要求 + 结论先行",
    283: "清理不要自动回收站兜底",
    366: "不要天天问，执行层做完再报",
    408: "发布按里程碑",
}


def _backup(root: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = Path(root) / "_hub" / "brain-backups" / f"brain-pre-rulebook-{stamp}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(Path(root) / "_hub" / "brain.db"))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return str(dest)


def _plan(root: str) -> list:
    todo = []
    with brain.db_conn(root) as conn:
        for mid, why in TARGETS.items():
            r = conn.execute("SELECT id, kind, tags, content FROM memories WHERE id=?",
                             (mid,)).fetchone()
            if not r:
                todo.append({"id": mid, "why": why, "missing_row": True})
                continue
            tags = r["tags"] or ""
            have = brain._tag_tokens(tags)  # noqa: SLF001
            todo.append({"id": mid, "why": why, "tags": tags,
                         "content": (r["content"] or "")[:60],
                         "needed": "必做" not in have})
    return todo


def apply(root: str, todo: list, backup_path: str) -> int:
    n = 0
    with brain.db_conn(root) as conn:
        for item in todo:
            if item.get("missing_row") or not item.get("needed"):
                continue
            row = conn.execute("SELECT tags FROM memories WHERE id=?",
                               (item["id"],)).fetchone()
            if not row:
                continue
            old = row["tags"] or ""
            parts = [t.strip() for t in old.split(",") if t.strip()]
            if "必做" in {t.lower() for t in parts}:
                continue
            new = brain._register_tags(conn, brain._norm_tags(  # noqa: SLF001
                ",".join(parts + ["必做"])))
            conn.execute("UPDATE memories SET tags=? WHERE id=?", (new, item["id"]))
            brain._journal_append(conn, brain._now(), "cli", "rulebook_tag",  # noqa: SLF001
                                  f"memories#{item['id']}", backup_path,
                                  f"tags: {old[:120]} -> {new[:120]}")
            n += 1
    return n


def verify(root: str) -> dict:
    with brain.db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, tags FROM memories WHERE tags LIKE '%必做%'")]
    exact = [r["id"] for r in rows if "必做" in brain._tag_tokens(r["tags"])]  # noqa: SLF001
    return {"like": len(rows), "exact": sorted(exact)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="R882 共享规则簿：把用户级硬规则标成 必做")
    ap.add_argument("--root", default="", help="大脑根目录，默认读配置")
    ap.add_argument("--apply", action="store_true", help="真写（先整库备份 + 逐条流水）")
    args = ap.parse_args(argv)
    root = (args.root or (core.load_config() or {}).get("root", "") or "").strip()
    if not root or not Path(root, "_hub", "brain.db").exists():
        print(f"根目录或 brain.db 不存在：{root or '(空)'}")
        return 2
    todo = _plan(root)
    print(f"目标 {len(TARGETS)} 条：")
    for it in todo:
        if it.get("missing_row"):
            print(f"  #{it['id']} 缺失（跳过）")
            continue
        mark = "需补" if it["needed"] else "已有"
        print(f"  #{it['id']} [{mark}] {it['why']} | tags={it['tags']} | {it['content']}")
    n_need = sum(1 for it in todo if it.get("needed"))
    print(f"\n待补 {n_need} 条。预演结束（没写任何东西）。")
    if not args.apply:
        print("确认后加 --apply（先整库备份，再逐条记流水）。")
        return 0
    if n_need == 0:
        print("无需回填；当前规则簿已就绪。")
        return 0
    bak = _backup(root)
    print(f"已备份：{bak}")
    n = apply(root, todo, bak)
    v = verify(root)
    print(f"回填完成：改 {n} 条；tags 含必做 {v['like']} 行，精确匹配 {len(v['exact'])} 条：{v['exact']}")
    print(f"回滚：用 {bak} 覆盖 _hub/brain.db（停 MCP 后操作），或按 journal action=rulebook_tag 逐条还原。")
    return 0 if n == n_need else 1


if __name__ == "__main__":
    raise SystemExit(main())
