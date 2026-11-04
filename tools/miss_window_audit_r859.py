# -*- coding: utf-8 -*-
"""核对 B066 的量化说法：那 4 条 URL 签名到底在不在当前命中率窗口里。

之所以要单独核：`searches` 里出现的 URL 形态签名是**全史**查出来的（2026-11-03 12:05），
而命中率分母走的是双闸门 `floor = max(近7天, 口径锚点)`。锚点若在 11-03 之后，这几条
根本不在分母里，「4/15=27%」这句就是错的，必须改成「锚点前遗留、不影响当前读数」。
只读，mode=ro。
"""
import sqlite3
from pathlib import Path

import _console  # noqa: F401  控制台 UTF-8 守卫（test_console_guard 盯）

DB = Path(r"D:\agent资料\zcode项目记录\_hub\brain.db")


def main() -> int:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    print("== 口径锚点 ==")
    for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'caliber:%'"):
        print(f"  {r['key']} = {r['value']}")
    print("== error_recall_miss 全量按 ts（含签名形态标注）==")
    for r in conn.execute(
        "SELECT ts, agent, query, hits FROM searches "
        "WHERE tool='error_recall_miss' ORDER BY ts"
    ):
        shape = "URL" if ("http:/ " in r["query"] or "https:" in r["query"]) else "   "
        print(f"  [{shape}] {r['ts']} {r['agent'] or '-':<10} {r['query'][:64]}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
