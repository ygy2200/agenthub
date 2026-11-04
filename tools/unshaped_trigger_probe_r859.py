# -*- coding: utf-8 -*-
"""B062 前置测量：那 16 次「没形状」的触发是什么，以及归因三分能不能只用现有表算（R859）。

为什么要先量：R858 把命中率的分母收窄成 `asked_shaped`（只算抽得出签名词的触发）之后，
`miss_unshaped` 变成一块看得见但说不出是什么的余量。欠账归因（①没解决 / ②解决了没登记 /
③已登记）要求知道「那次触发到底是不是一条真报错」，而 `(no_signature)` 在库里只有
`searches` 一行、query 就是字面量——`_log_error_miss` 对没签名的情况显式不挂 journal 行，
正文也没落库。所以本探针做的是**可行性判定**，不是统计：能不能从现存数据反推，还是只能
往前补采集。

只读打开生产库（mode=ro），不写任何东西。
"""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

import _console  # noqa: F401  控制台 UTF-8 守卫（test_console_guard 盯）

ROOT = Path(r"D:\agent资料\zcode项目记录")
DB = ROOT / "_hub" / "brain.db"


def main() -> int:
    if not DB.exists():
        print(f"FAIL 库不存在：{DB}")
        return 2
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # 必须和 brain._now() 同格式（ISO 带 T）。写成空格分隔会把比较基准整体抬高——
    # 'T'(0x54) > ' '(0x20)，同一天里所有真实行都会被算进窗口，实测多算 ~1 天。
    # 真库 ts 全表都是这个格式（实测见下面的 sample），所以口径本身可对齐。
    floor = (dt.datetime.now() - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    print("== 全表 ts 格式抽样（证明窗口比较没有混格式）==")
    for r in conn.execute(
        "SELECT ts FROM searches WHERE tool='error_recall_miss' ORDER BY id DESC LIMIT 2"
    ):
        print("  sample ts:", r["ts"])
    print(f"  window floor: {floor}")

    def by_agent(where: str, label: str) -> int:
        total = 0
        for r in conn.execute(
            "SELECT agent, COUNT(*) AS n FROM searches "
            f"WHERE tool='error_recall_miss' AND ts>=? AND {where} "
            "GROUP BY agent ORDER BY n DESC",
            (floor,),
        ):
            print(f"  {label} {r['agent'] or '(empty)'} {r['n']}")
            total += r["n"]
        return total

    print("== 近 7 天按 agent ==")
    un = by_agent("query='(no_signature)'", "unshaped")
    keyed = by_agent("query<>'(no_signature)' AND query<>''", "keyed   ")
    print(f"  合计 unshaped={un} keyed={keyed}")

    print("== 全史按月份（看这块余量是一直有还是新长出来的）==")
    for r in conn.execute(
        "SELECT substr(ts,1,7) AS m, COUNT(*) AS n FROM searches "
        "WHERE tool='error_recall_miss' AND query='(no_signature)' GROUP BY m ORDER BY m"
    ):
        print(f"  {r['m']} {r['n']}")

    print("== 反推素材检查：journal 里有没有同窗口的 brain_error_miss 可对上 ==")
    j = conn.execute(
        "SELECT COUNT(*) AS n FROM journal WHERE action='brain_error_miss'"
    ).fetchone()["n"]
    print(f"  journal brain_error_miss 总行数={j}")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(searches)")]
    print(f"  searches 列={cols}")
    extra = [c for c in cols if c not in ("ts", "agent", "tool", "query")]
    if extra:
        print(f"  可能带正文的列：{extra}（逐条看有没有内容）")
        for r in conn.execute(
            f"SELECT ts, agent, {', '.join(extra)} FROM searches "
            "WHERE tool='error_recall_miss' AND query='(no_signature)' AND ts>=?"
            " ORDER BY ts DESC LIMIT 5",
            (floor,),
        ):
            print("   ", dict(r))
    else:
        print("  searches 只有 ts/agent/tool/query(+hits)：没形状的那次触发正文没落库"
              "——反推不可行，只能往前补采集。")

    print("== 归因三分能不能只用 searches 算出来（不看会话正文）==")
    # 口径：某条 miss 签名之后，同签名的**后续事件**是什么——
    #   ①复发且仍没答案 = 后面还有同签名的 error_recall_miss（确定没解决）
    #   ③后来有答案     = 后面出现同签名且 hits>0 的 error_recall（登记过、现在会命中）
    #   ②判不了         = 之后再没有同签名事件（撞过一次就没再来，解决与否无从推断）
    rows = conn.execute(
        "SELECT query, MIN(ts) AS first_ts, COUNT(*) AS n FROM searches "
        "WHERE tool='error_recall_miss' AND query<>'(no_signature)' AND query<>'' "
        "GROUP BY query ORDER BY n DESC, first_ts"
    ).fetchall()
    c1 = c3 = c2 = 0
    for r in rows:
        later = conn.execute(
            "SELECT tool, hits FROM searches WHERE query=? AND ts>?",
            (r["query"], r["first_ts"]),
        ).fetchall()
        if any(x["tool"] == "error_recall" and x["hits"] > 0 for x in later):
            c3 += 1
            tag = "③已登记"
        elif any(x["tool"] == "error_recall_miss" for x in later):
            c1 += 1
            tag = "①复发仍没答案"
        else:
            c2 += 1
            tag = "②判不了"
        print(f"  {tag:<12} ×{r['n']} {r['query'][:60]}")
    print(f"  合计：①复发 {c1} / ②判不了 {c2} / ③已登记 {c3}（按签名去重，共 {len(rows)} 类）")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
