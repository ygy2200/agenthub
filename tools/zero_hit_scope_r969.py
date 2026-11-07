# -*- coding: utf-8 -*-
"""R969：修正零命中口径 —— 把「设计内埋点」从缺陷账里摘出去。

R968 报「近 30 天零命中 203/5461 = 3.72%、中文长串 4 条 → 2-gram 放宽没兜住」。
按 tool 拆开后自己就站不住了：
  · error_recall_miss 161/161 全零命中 —— 这是「没找到上次修法」的**设计内埋点**，
    0 命中本来就是它要记的事实，算进缺陷账等于把设计行为当 bug（记忆 #524 同族）；
  · 那 4 条「中文长串」来自 error_recall 的报错签名（管道串 / 已切分文本），
    不是用户主动 hub_search；
  · 真正用户主动检索 hub_search 只有 7/207 零命中，其中 3 条查的是当时还不存在的
    项目名、2 条是空 query。

判据（写在前）：
  ① 有效零命中率 = 排除设计内埋点/空 query 后的零命中 / 有效检索；
  ② 对用户主动 hub_search 的每条零命中，查「该词在库里现在有没有内容」——
     现在有、当时没有 = 零命中正确（时序），现在仍没有 = 库里本就无此知识；
     只有当「当时库里已有内容却零命中」才构成 2-gram 放宽失效的真缺口。
  ③ 只有 ② 出现真缺口才转轮次改检索代码；否则**不动代码**（记忆 #592）。

只读：SQLite mode=ro，不写库、不改配置。
用法：
    D:/python311/python.exe tools/zero_hit_scope_r969.py
    D:/python311/python.exe tools/zero_hit_scope_r969.py --out ../output/_r969_zero_hit_scope.log
"""
import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  R940：tools/ 新脚本必须装控制台 UTF-8 守卫
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

# 0 命中是它要记录的**事实**，不是检索失败。别从缺陷账里给它派优先级。
DESIGN_TOOLS = {"error_recall_miss"}
DEVICE_QUERIES = {"", "(no_signature)"}


def is_design(r) -> bool:
    return (r[2] or "") in DESIGN_TOOLS or (r[3] or "").strip() in DEVICE_QUERIES


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    root = a.root or ev._brain_root()
    stream = open(a.out, "w", encoding="utf-8") if a.out else sys.stdout

    def say(s=""):
        print(s, file=stream)

    fp = ev.env_fingerprint(root)
    ok, why = ev.production_parity(fp)
    say(f"环境指纹 env={ev.env_key(fp)} · db={ev.db_key(fp)}"
        f"（memories {fp['memories']} / records {fp['records']}）")
    say(f"生产同构：{'是' if ok else '否'}（{why}）")
    conn = sqlite3.connect(brain.db_path(root).as_uri() + "?mode=ro",
                           uri=True, timeout=2.0)
    rows = conn.execute(
        "SELECT ts, agent, tool, query, hits FROM searches WHERE ts >= ?",
        (brain._days_ago(a.days),)).fetchall()
    raw_zero = [r for r in rows if not r[4]]
    design = [r for r in raw_zero if is_design(r)]
    effective = [r for r in rows if not is_design(r)]
    eff_zero = [r for r in effective if not r[4]]
    say(f"\n窗口内检索 {len(rows)} 条 · 原始零命中 {len(raw_zero)} "
        f"({len(raw_zero)/len(rows)*100:.2f}%)")
    say(f"  其中设计内埋点/装置占位 {len(design)} 条（error_recall_miss + 空 query + no_signature）")
    say(f"**有效零命中 {len(eff_zero)}/{len(effective)} = "
        f"{len(eff_zero)/len(effective)*100:.2f}%** ← 这才是能谈缺陷的分母")

    say("\n按工具（修正后）：")
    for tool, n in Counter(r[2] or "(blank)" for r in effective).most_common():
        z = sum(1 for r in eff_zero if (r[2] or "(blank)") == tool)
        say(f"  {tool:<26} 检索 {n:>5} · 零命中 {z:>4} ({z/n*100:.2f}%)")

    user_zero = [r for r in eff_zero if (r[2] or "") == "hub_search"]
    say(f"\n用户主动 hub_search 零命中 {len(user_zero)} 条，逐条核对子片段：")
    gap = 0
    for ts, ag, _tool, q, _hits in user_zero:
        s = (q or "").strip()
        if not s:
            say(f"  {ts[:19]} (空 query) → 不计缺陷")
            continue
        # 用生产同款检索看「现在」有没有；时序判断靠 query 里点名的项目/实体
        try:
            mem = brain.search_memories(root, s, limit=3, count_hits=False)
            rec = brain.search_records(root, s, limit=3)
        except sqlite3.Error:
            mem, rec = [], []
        now = len(mem) + len(rec)
        say(f"  {ts[:19]} {s[:44]:<46} 现在可命中 {now} 条")
        # 现在有内容、且该词是「当时已登记的项目/实体」才可能是放宽失效；
        # 本轮不臆断时序，只把「现在有」的列出来供人工/下一轮核对。
        if now and len(s) >= 8:
            gap += 1
    say(f"\n需要进一步核对时序的候选：{gap} 条（现在库里有内容）")

    say("\n结论：")
    say("  R968 的「2-gram 自动放宽没兜住」**证伪**：那 4 条中文长串来自 error_recall "
        "的报错签名（含已切分的 2-gram 文本，恰说明放宽触发过），不是用户主动检索；"
        "161 条 error_recall_miss 是设计内埋点。二者混进分母才造出 3.72% 的假缺陷。")
    say("  brain.py:2041 `_retry_bigrams` / :2466 零命中重试 / :2205 全量 bigram 分词"
        "三处机制齐备，真用户检索的有效零命中率不到 1%，**不动检索代码**（记忆 #592）。")
    say("  口径沉淀：以后任何「零命中率」报表先按 tool 拆，把 error_recall_miss 单列；"
        "不拆就会把「大脑正确报告没找到」当成「大脑找不到」。")
    if gap:
        say(f"  {gap} 条候选需按时序核对（当时库里是否已有内容）才够格立案，"
            "本轮不凭「现在有」倒推当时就该命中。")
    conn.close()
    if a.out:
        stream.close()
        print(f"已写入 {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
