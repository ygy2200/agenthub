# -*- coding: utf-8 -*-
"""旧记忆的签名词回填（B049）：把「错误形状」沉淀进 tags，让同签名报错确定命中。

为什么需要：R834（B041）只在新写入时沉淀签名。此前写下的记忆，下次同签名报错想被
召回，只能指望正文恰好出现了那串字——命中靠运气。回填把运气变成确定。

三条纪律：
  · 默认 dry-run，只报会改哪几条、改什么；--apply 才写；
  · 写前用 sqlite backup API 整库备份（在线安全），每条改动进审计流水（含旧 tags）；
  · 只动 tags 一列：`updated` 不碰（碰了这些旧记忆会装成"新记忆"，新鲜度与陈旧巡检
    全被污染）——这是回填最容易造成的静默事故。

用法：
    python tools/sig_backfill_r843.py                 # 预演
    python tools/sig_backfill_r843.py --apply         # 真写（先备份）
    python tools/sig_backfill_r843.py --measure       # 回填前后各跑一次同签名召回，出对照
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brain  # noqa: E402
import core  # noqa: E402
import _console  # noqa: E402,F401  控制台 UTF-8 守卫（R179）

core.JOURNAL_SINK = brain.journal_add


def _tag_worthy(term: str) -> bool:
    """这个词够不够格进标签库（回填比写入更严——批量写错会一次性污染词表）。

    预演实测的反例：#21 抽出 `skills-20261001-185820`（备份目录名）、#22 抽出
    `network\\windows-nic-switching`（路径碎片）、#24 抽出 `save_state(deleted)`（调用表达式）。
    这些不是「这条报错的身份」，是「当时现场的位置」——回填进去等于给标签库灌垃圾，
    而标签库是给人看的。写入路径靠 _sediment_tags 只挡空格/非 ASCII，挡不住这一层。
    """
    import re
    t = str(term or "")
    if not t or len(t) < 3 or len(t) > 48:
        return False
    if re.search(r"[\\/()\[\]{}<>\"'=:;,*?|]", t):
        return False              # 路径/调用式/多值串：位置，不是身份
    if re.search(r"\d{6,}", t):
        return False              # 时间戳/长编号（0x28b52ffd 由 error_signature_terms 单独处理）
    if not re.fullmatch(r"[a-z][a-z0-9_.+-]*", t):
        return False
    return True


def _sig_terms_for(content: str) -> list:
    """这条正文该沉淀哪些签名词：w>=3 的招牌、ASCII 单词形、且够格进标签库。

    R856：与 `error_sig_of` 用同一把尺子——先 `_error_span` 剪掉说明文字。记忆正文普遍是
    「报错原文 + 当时怎么查的」，不剪就会把 `brain.db`、`last_ts` 这类**说明里的标识符**
    灌进标签库（批量写错一次污染一整批，正是 `_tag_worthy` 那段注释防的那类）。
    """
    terms = brain.error_signature_terms(                    # noqa: SLF001
        brain._error_voice(brain._error_span(content or "")))  # noqa: SLF001
    out, seen = [], set()
    for t, w in terms:
        if w < 3 or not t or " " in t or not t.isascii() or t in seen:
            continue
        if not _tag_worthy(t):
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= brain.MEMORY_SIG_TAG_MAX:
            break
    return out


def _tag_set(tags: str) -> set:
    import re
    return {t.strip().lower() for t in re.split(r"[,，]", str(tags or "")) if t.strip()}


def scan(root: str) -> list:
    """返回需要回填的记忆：有错误形状、但招牌词没进 tags。

    双重把关：`_tag_worthy` 管形状（不是路径/时间戳/调用式），文档频率管区分度——
    一个词如果在全库几十条记忆里都出现（agenthub / config.json / json / skills），
    它不指向任何一条具体报错，塞进标签库只会让人更难按标签找人。
    「够不够独一份」不用人肉维护停用词表，按语料自己算（df 上限 = 2% 或 12 条取大）。
    """
    with brain.db_conn(root) as conn:
        rows = conn.execute(
            "SELECT id, kind, content, tags, project, agent FROM memories "
            "WHERE COALESCE(status,'active')<>'deleted'").fetchall()
    corpus = [str(r["content"] or "").lower() for r in rows]
    df_cap = max(12, int(len(corpus) * 0.02))
    todo = []
    for r in rows:
        terms = _sig_terms_for(r["content"] or "")
        if not terms:
            continue
        have = _tag_set(r["tags"])
        keep = []
        for t in terms:
            if t in have:
                continue
            if sum(1 for c in corpus if t in c) > df_cap:
                continue          # 到处都是的词不是身份
            keep.append(t)
        if not keep:
            continue
        todo.append({"id": int(r["id"]), "kind": r["kind"], "project": r["project"] or "",
                     "agent": r["agent"] or "", "tags": r["tags"] or "",
                     "terms": terms, "missing": keep})
    return todo


def _backup(root: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = Path(root) / "_hub" / "brain-backups" / f"brain-pre-sigbackfill-{stamp}.db"
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


def apply(root: str, todo: list, backup_path: str) -> int:
    n = 0
    with brain.db_conn(root) as conn:
        for item in todo:
            row = conn.execute("SELECT tags FROM memories WHERE id=?",
                               (item["id"],)).fetchone()
            if not row:
                continue
            old = row["tags"] or ""
            have = [t for t in old.split(",") if t.strip()]
            new = brain._register_tags(conn, brain._norm_tags(  # noqa: SLF001
                ",".join(have + item["missing"])))
            if new == old:
                continue
            conn.execute("UPDATE memories SET tags=? WHERE id=?", (new, item["id"]))
            brain._journal_append(conn, brain._now(), "cli", "sig_backfill",  # noqa: SLF001
                                  f"memories#{item['id']}", backup_path,
                                  f"tags: {old[:120]} → {new[:120]}")
            n += 1
    return n


def measure(root: str, todo: list, mode: str = "search") -> dict:
    """「拿招牌词去检索，这条记忆排第几」——回填净效果的可用量尺。

    试过两种更直觉的指标，都不成立，记在这里免得下次再走一遍：
      · 拿正文自召回（error_recall(body)）：70/77 早就是命中——正文里本来就有那串字，
        测的是「库能不能搜到自己」，不是「标签有没有帮忙」。
      · 拿招牌词裸查（error_recall("typeerror")）：0/77——裸词不像报错，精度闸门在
        检索之前就拒了，测的是闸门不是标签。
    所以改用普通检索的排名：tags 本来就参与打分（search_memories），回填前靠正文
    恰好含词，回填后 tags 兜底。排行榜变化是可归因的。
    """
    old_bump = brain._bump_inject                                  # noqa: SLF001
    brain._bump_inject = lambda *a, **k: None                     # noqa: SLF001
    top3 = top10 = 0
    missed = []
    try:
        for item in todo:
            res = brain.search_memories(root, " ".join(item["terms"]), limit=20,
                                        count_hits=False)
            ids = [int(r.get("id") or 0) for r in res]
            if item["id"] in ids:
                rank = ids.index(item["id"]) + 1
                if rank <= 3:
                    top3 += 1
                if rank <= 10:
                    top10 += 1
                else:
                    missed.append(item["id"])
            else:
                missed.append(item["id"])
    finally:
        brain._bump_inject = old_bump                              # noqa: SLF001
    return {"total": len(todo), "top3": top3, "top10": top10, "missed": missed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="旧记忆签名词回填（默认预演）")
    ap.add_argument("--root", default="")
    ap.add_argument("--apply", action="store_true",
                    help="真写（先整库备份）。默认只补「招牌词检索进不了前 10」的那些——"
                         "已经能稳定召回的条目加标签只会污染词表，不会改变结果")
    ap.add_argument("--apply-all", action="store_true",
                    help="连已经能召回的一起补（想做全量词表统一时用）")
    ap.add_argument("--measure", action="store_true", help="回填前后的同签名召回对照")
    ap.add_argument("--show", type=int, default=8, help="预演时列几条样例")
    ap.add_argument("--json", default="", help="明细写文件")
    args = ap.parse_args(argv)

    root = (args.root or (core.load_config() or {}).get("root", "") or "").strip()
    if not root or not Path(root).is_dir():
        print(f"根目录不可用：{root or '(空)'}")
        return 2
    todo = scan(root)
    print(f"扫描完成：{len(todo)} 条记忆有错误形状但缺签名 tag")
    for item in todo[:args.show]:
        print(f"  · #{item['id']} [{item['kind']}] 补 {item['missing']}"
              f" · 现 tags={item['tags'][:50] or '(空)'}"
              f" · {str(item['project'])[:18]}")
    if todo and len(todo) > args.show:
        print(f"  …还有 {len(todo) - args.show} 条")
    before = None
    if args.measure:
        before = measure(root, todo)
        print(f"  回填前：招牌词检索进前 3 的 {before['top3']}/{before['total']}"
              f"（{round(before['top3'] / max(1, before['total']) * 100, 1)}%）"
              f" · 进前 10 的 {before['top10']}")
    if not (args.apply or args.apply_all):
        print("预演结束（没写任何东西）。确认后加 --apply（只补进不了前 10 的）"
              "或 --apply-all（全补），都会先整库备份再逐条记流水。")
        if args.json:
            Path(args.json).write_text(json.dumps(todo, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
            print(f"  明细：{args.json}")
        return 0
    if not todo:
        print("没有要回填的，退出。")
        return 0
    todo_all = list(todo)
    if args.apply and not args.apply_all and before is not None:
        need = set(before["missed"])
        skipped = [x for x in todo if x["id"] not in need]
        todo = [x for x in todo if x["id"] in need]
        if skipped:
            print(f"  跳过 {len(skipped)} 条已能召回前 10 的（加标签只污染词表，不改变结果）")
        if not todo:
            print("没有需要真补的条目。")
            return 0
    bak = _backup(root)
    print(f"已备份：{bak}")
    n = apply(root, todo, bak)
    print(f"回填完成：改 {n} 条（每条一行 sig_backfill 流水，按 backup 路径可整体回滚）")
    left = scan(root)
    print(f"复核：仍缺签名 tag 的 {len(left)} 条"
          + ("（非 0 说明有正文抽不出够格的招牌词，属正常残留）" if left else "（清零）"))
    if before is not None:
        after = measure(root, todo_all)
        print(f"  回填后：招牌词检索进前 3 的 {after['top3']}/{after['total']}"
              f"（{round(after['top3'] / max(1, after['total']) * 100, 1)}%）"
              f" · 进前 10 的 {after['top10']}")
        print(f"  净增：前 3 +{after['top3'] - before['top3']} · 前 10 +{after['top10'] - before['top10']}")
        if after["missed"]:
            print(f"  仍召不回：{after['missed'][:10]}"
                  + (f"（共 {len(after['missed'])} 条，多为签名被更近的同签名记忆抢走）"
                     if len(after["missed"]) > 10 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
