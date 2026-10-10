# -*- coding: utf-8 -*-
"""方案A：清理 2026-09-30 历史重复（每组 2 条、其中一条为 dsh）。

背景：这批重复产生于 2026-09-30T17:24:29（早于 2026-10-10 的去重键事故），
成因同为"去重键含 agent"——当天解析规则变动过，同一批源文件被两条规则
各插了一次（一条 agent=dsh、一条非 dsh）。每组两条 project/source/title/
content[:256] 完全相同，删 dsh 副本不丢任何内容。

策略（用户拍板「方案A」）：
  · 判定条件：同 (project, source, title, content[:256]) 恰好 2 条，
    且恰好 1 条 agent='dsh' → 删除 dsh 那条，保留非 dsh 那条。
  · 结构不符的组（两条都 dsh / 无 dsh / >2 条）一律跳过并报告。
  · 每条删除写 journal（action='dedup_history_dsh'，整行 JSON 可还原）。

用法：
  python fix_dups_dsh.py              # 只读试算（默认）
  python fix_dups_dsh.py --apply --yes  # 执行
"""
import json
import os
import sqlite3
import sys
from collections import defaultdict

ROOT = r"D:\agent资料\zcode项目记录"
DB = os.path.join(ROOT, "_hub", "brain.db")


def main():
    apply = "--apply" in sys.argv
    yes = "--yes" in sys.argv
    if apply and not yes:
        print("拒绝执行：--apply 需要同时带 --yes 确认")
        return 1

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM records WHERE status='active'")]
    print(f"活跃记录总数: {len(rows)}")

    groups = defaultdict(list)
    for r in rows:
        key = (r["project"], r["source"], r["title"], (r["content"] or "")[:256])
        groups[key].append(r)
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"重复组总数: {len(dups)}")

    to_delete, skipped = [], []
    for k, v in dups.items():
        dsh_rows = [x for x in v if (x["agent"] or "") == "dsh"]
        non_dsh = [x for x in v if (x["agent"] or "") != "dsh"]
        if len(v) == 2 and len(dsh_rows) == 1 and len(non_dsh) == 1:
            to_delete.append(dsh_rows[0])
        else:
            skipped.append((k, v))

    print(f"\n可清理（删 dsh 副本）: {len(to_delete)} 条")
    print(f"跳过（结构不符）: {len(skipped)} 组")
    for k, v in skipped[:10]:
        print(f"  跳过: {k[0][:30]} | agents={[x['agent'] for x in v]}")

    if to_delete:
        from collections import Counter
        by_date = Counter((x["created"] or "")[:10] for x in to_delete)
        print("\n按 created 日期分布:")
        for d, n in by_date.most_common():
            print(f"  {d}: {n} 条")
        by_proj = Counter(x["project"] for x in to_delete)
        print(f"涉及项目: {len(by_proj)} 个")
        print("\n样本（前 5 条将删除的）:")
        for x in to_delete[:5]:
            print(f"  #{x['id']} dsh | {(x['title'] or '')[:50]}")

    # dsh 记录数前后对比
    dsh_before = sum(1 for r in rows if (r["agent"] or "") == "dsh")

    if not apply:
        print(f"\n[试算模式] 未写库。dsh 记录数: {dsh_before} → {dsh_before - len(to_delete)}")
        print("加 --apply --yes 执行")
        return 0

    from datetime import datetime
    ts = datetime.now().isoformat(timespec="seconds")
    with c:
        for t in to_delete:
            c.execute(
                "INSERT INTO journal(ts,agent,action,target,backup,note) "
                "VALUES(?,?,?,?,?,?)",
                (ts, "system", "dedup_history_dsh",
                 f"brain:records#{t['id']}", json.dumps(t, ensure_ascii=False),
                 "方案A：2026-09-30 历史重复，删 dsh 副本保留非 dsh（内容逐字段相同）"))
        c.executemany("DELETE FROM records WHERE id=?",
                      [(t["id"],) for t in to_delete])

    after = [dict(r) for r in c.execute(
        "SELECT * FROM records WHERE status='active'")]
    dsh_after = sum(1 for r in after if (r["agent"] or "") == "dsh")
    print(f"\n[已执行] 删除 {len(to_delete)} 条")
    print(f"活跃记录: {len(rows)} → {len(after)}")
    print(f"dsh 记录: {dsh_before} → {dsh_after}")

    # 复核：剩余重复组
    g2 = defaultdict(list)
    for r in after:
        g2[(r["project"], r["source"], r["title"], (r["content"] or "")[:256])].append(r)
    left = sum(1 for v in g2.values() if len(v) > 1)
    print(f"剩余重复组: {left}（预期 0）")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
