# -*- coding: utf-8 -*-
"""R857 配套：把「签名逐字相同」这条强通道在生产上真的接上（回填 errors.sig）。

为什么要它（R856 收尾量出来的分母）：`errors.sig` 列是 R843 才建的，真库 20 行里 **19 行是空的**，
唯一带值的 #20 存的还是被说明文字带偏的旧口径键（`brain.db|created_at|last_detail|last_ts`）。
`_error_covered` 的第一道判据是「欠账键 == errors.sig」——两边不可能相等，于是 R834 承诺的
「登记一次、下次同签名秒命中」在生产上一次都没成立过：R856 之后那几条能判成 resolved，
走的**全是**模糊 LIKE 通道。旧行也不会自愈：`error_add` 只在「合并且 sig 为空」时补写。

两个限制（沿记忆 #527：拿名字猜只能用于一次性回填）：
  ① 默认**只补空 sig**——非空值意味着当初有人算过它，覆盖它得逐条点名（`--overwrite-ids`）。
  ② 默认预演，`--apply` 才写；写入前在线备份整库，每行留一条「错误签名」流水写明改前值。

用法：
    python tools/err_sig_backfill.py                      # 看有多少行要补（不改库）
    python tools/err_sig_backfill.py --apply               # 补空 sig（自动备份 + 逐行流水）
    python tools/err_sig_backfill.py --overwrite-ids 20 --apply   # 点名覆盖指定行
"""
from __future__ import annotations

import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _console  # noqa: E402,F401  控制台 UTF-8 守卫（Windows GBK 管道下 print 中文会崩）
import brain  # noqa: E402
import core  # noqa: E402


def plan(root: str, overwrite_ids: list[int]) -> list[dict]:
    """算出每行该写什么键：空 sig 一律补；非空 sig 只有在点名名单里才允许覆盖。"""
    with brain.db_conn(root) as conn:
        rows = list(conn.execute(
            "SELECT id, ts, title, detail, undo, sig FROM errors "
            "WHERE COALESCE(status,'open')<>'deleted' ORDER BY id"))
    out = []
    for r in rows:
        fresh = brain.error_sig_of(r["detail"] or "") or brain.error_sig_of(r["title"] or "")
        old = r["sig"] or ""
        if not fresh:
            # 抽不出键：补不了，但必须报出来——静默消失等于把分母吃掉（R856 同族）
            if old:
                out.append({"id": r["id"], "old": old, "new": "", "act": "keep-old",
                            "title": r["title"]})
            else:
                out.append({"id": r["id"], "old": "", "new": "", "act": "skip-nokey",
                            "title": r["title"]})
            continue
        if old == fresh:
            continue                                   # 已经是新口径的键
        if old:
            if r["id"] not in overwrite_ids:
                out.append({"id": r["id"], "old": old, "new": fresh, "act": "needs-naming",
                            "title": r["title"]})
                continue
            act = "overwrite"
        else:
            act = "fill"
        out.append({"id": r["id"], "old": old, "new": fresh, "act": act, "title": r["title"]})
    return out


def _backup(root: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = Path(root) / "_hub" / "brain-backups" / f"brain-pre-errsig-{stamp}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(Path(root) / "_hub" / "brain.db"))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)          # 在线备份：WAL 下也是完整快照
        finally:
            dst.close()
    finally:
        src.close()
    return str(dest)


def report(items: list[dict]) -> None:
    fill = [x for x in items if x["act"] == "fill"]
    over = [x for x in items if x["act"] == "overwrite"]
    pend = [x for x in items if x["act"] == "needs-naming"]
    keep = [x for x in items if x["act"] == "keep-old"]
    nope = [x for x in items if x["act"] == "skip-nokey"]
    print(f"待补空 sig {len(fill)} 行 · 点名覆盖 {len(over)} 行 · "
          f"非空但与新键不符（未点名，不动）{len(pend)} 行 · "
          f"按新口径抽不出键（保留旧值）{len(keep)} 行 · 空且抽不出键 {len(nope)} 行")
    marks = {"fill": "✎补", "overwrite": "✎覆", "needs-naming": "· 未点名",
             "keep-old": "· 留旧", "skip-nokey": "· 无键"}
    for x in fill + over + pend + keep + nope:
        print(f"  {marks[x['act']]} errors#{x['id']}  {(x['title'] or '')[:34]}"
              f"\n        旧={x['old'] or '(空)'}\n        新={x['new'] or '(抽不出)'}")


def apply(root: str, items: list[dict], agent: str) -> int:
    """写入并逐行留流水；返回**改了多少行**（调用方拿它做验收，别当退出码）。"""
    todo = [x for x in items if x["act"] in ("fill", "overwrite")]
    if not todo:
        print("没有可写的行。")
        return 0
    bak = _backup(root)
    print(f"整库备份 -> {bak}")
    n = 0
    with brain.db_conn(root) as conn:
        for x in todo:
            cur = conn.execute("SELECT sig FROM errors WHERE id=?", (x["id"],)).fetchone()
            if not cur or (cur["sig"] or "") != x["old"]:
                print(f"  ! errors#{x['id']} 在这期间被改过"
                      f"（现值={cur['sig'] if cur else '无此行'}），跳过")
                continue
            conn.execute("UPDATE errors SET sig=? WHERE id=?", (x["new"], x["id"]))
            brain._journal_row(conn, agent, "错误签名", f"errors#{x['id']}",        # noqa: SLF001
                               f"回填签名（R857）：改前 sig={x['old'] or '(空)'} -> {x['new']}；"
                               f"回滚=把 sig 改回原值，或整库还原 {bak}")
            n += 1
    print(f"写入 {n} 行（每行一条「错误签名」流水）")
    return n


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="回填 errors.sig（默认只补空值、只预演）")
    ap.add_argument("--root", default="", help="大脑根目录（缺省读 ~/.agenthub/config.json）")
    ap.add_argument("--overwrite-ids", default="", help="允许覆盖非空 sig 的行号，逗号分隔")
    ap.add_argument("--agent", default="qoder", help="流水署名")
    ap.add_argument("--apply", action="store_true", help="真写（缺省只预演）")
    args = ap.parse_args(argv[1:])

    root = args.root or core.get_root()
    if not root or not Path(root, "_hub", "brain.db").exists():
        print(f"根目录无效或没有库：{root}")
        return 2
    ids = [int(x) for x in str(args.overwrite_ids).replace("，", ",").split(",")
           if x.strip().isdigit()]
    items = plan(root, ids)
    report(items)
    if not args.apply:
        print("预演结束，未改库。加 --apply 写入（自动整库备份）。")
        return 0
    apply(root, items, args.agent)          # 返回值是「改了几行」，不是退出码
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
