# -*- coding: utf-8 -*-
"""R925 前后对照：同一批真库记忆，旧 fp（账本里存的）与新 fp（净化后现抽）比不可匹配数。"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))
import _console  # noqa: E402,F401
import brain
import core

NEVER = re.compile(r"[`'\"“”‘’（）()【】〔〕「」『』，。、；：！？…\s]")


def main() -> int:
    root = (core.load_config() or {}).get("root", "")
    if not root:
        print("读不到大脑根目录")
        return 1
    with brain.db_conn(root) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT memory_id FROM recall_adherence"
            " ORDER BY memory_id DESC LIMIT 400")]
        mem = {r[0]: dict(r) for r in conn.execute(
            "SELECT id, content, tags FROM memories")}
        last_fp = {}
        for r in conn.execute("SELECT memory_id, fp FROM recall_adherence ORDER BY ts DESC"):
            last_fp.setdefault(r[0], r[1])

    tot_old = tot_new = affected = shown = 0
    for mid in ids:
        m = mem.get(mid)
        olds = []
        try:
            olds = json.loads(last_fp.get(mid) or "[]")
        except ValueError:
            olds = []
        if not m or not olds:
            continue
        news = brain.recall_fingerprints(f"{m['content'] or ''} {m['tags'] or ''}",
                                         pool_texts=None, prompt="")
        ob = [t for t in olds if isinstance(t, str) and NEVER.search(t)]
        nb = [t for t in news if NEVER.search(t)]
        tot_old += len(ob)
        tot_new += len(nb)
        if ob:
            affected += 1
            if shown < 8:
                shown += 1
                print(f"#{mid:<5} 旧不可匹配 {len(ob)} 个 → 新 {len(nb)} 个")
                print(f"      旧：{ob[:1]}")
                print(f"      新：{news[:3]}")
    print(f"\n受影响记忆 {affected} 条 · 不可匹配 token 合计 旧 {tot_old} → 新 {tot_new}")
    print("口径：新 fp 用当前代码现抽、旧 fp 取账本里最近一笔，两者 token 数可能不同——"
          "这里只比「带反引号/中文标点」这一项，不比数量。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
