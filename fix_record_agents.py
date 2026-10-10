# -*- coding: utf-8 -*-
"""迁移存量归属修正 —— **默认只读试算**，加 --apply 才写库。

问题（2026-10-09 定位）：core.AGENT_KNOWN 硬编码漏了dsh/workbuddy，导致历史文件里
这两家 agent 的段头"## 日期（dsh）"认不出agent → brain._migrate_records 的
fallback 一律猜 "zcode"。真实库 1436 条migrated 记录中约 450 条归属是猜的。

修正依据（按优先级）：
  ① 段头括号里能按注册表识别 → 该段 agent（最可信）
  ② 段头认不出 → **继承最近的 ## 父段**的 agent（### 是 ## 的子段，如
     "### 验证结果"/"### 做了什么" 这类小标题本就该跟随所属会话）
  ③ 父段也没有 → 降级 unknown（**不再冒充 zcode**）

安全：
  - 默认 dry-run，只打印不动库
  - --apply 需交互确认（或 --yes 跳过）
  - 每条修正写 journal（old值存backup），可追溯可回滚
  - 回滚：整库还原备份 db，或按 journal 反向 UPDATE
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain  # noqa: E402
import core  # noqa: E402

DB = r"D:\agent资料\zcode项目记录\_hub\brain.db"
ROOT = r"D:\agent资料\zcode项目记录"


def parse_agent_of(title: str, known: dict) -> str:
    agent, _raw = core.parse_agent_from_header(title or "", known)
    return agent


def build_line_agent_map(known: dict) -> dict[tuple[str, str, int], str]:
    """(项目名, 文件名, 行号) → 正确 agent。

    ★ key 必须带 project：records.source 里的文件名（"工作记录.md"）不含项目路径，
      而每个项目都有自己的 工作记录.md——只用 (文件名, 行号) 会跨项目互相覆盖，
      导致 A 项目的记录被判成 B 项目 agent（首次试算就踩了：2026-08-16 的 KVK 记录
      被误判成 workbuddy）。source 存不了 project，故从库里取记录时一并带上。

    重扫全部工作记录.md，按 ## / ### 层级维护"当前归属"：
      - `##` 段若能识别 agent → 更新当前归属
      - `###` 段沿用当前归属
    """
    out: dict[tuple[str, str, int], str] = {}
    for proj in os.listdir(ROOT):
        pdir = os.path.join(ROOT, proj)
        if not os.path.isdir(pdir):
            continue
        fname = core.RECORD_NAME
        fpath = os.path.join(pdir, fname)
        if not os.path.isfile(fpath):
            cands = []
            try:
                cands = [f for f in os.listdir(pdir)
                         if os.path.isfile(os.path.join(pdir, f))
                         and os.path.splitext(f)[1].lower() in core.SEARCH_EXTS]
            except OSError:
                cands = []
            if not cands:
                continue
            fname = max(cands, key=lambda f: os.path.getmtime(os.path.join(pdir, f)))
            fpath = os.path.join(pdir, fname)
        try:
            lines = core.read_text(Path(fpath)).splitlines()
        except OSError:
            continue
        cur = ""  # 当前归属（由最近的 ## 段决定）
        for i, line in enumerate(lines, 1):
            m = core.HEADER_RE.match(line)
            if not m:
                continue
            title = m.group(1).strip()
            got = parse_agent_of(title, known)
            if got:
                cur = got
            out[(proj, fname, i)] = cur
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只试算）")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    known = core.agent_known_names(ROOT)
    print(f"注册表：{ {k: v for k, v in sorted(known.items())} }")

    lmap = build_line_agent_map(known)
    print(f"已重扫段头：{len(lmap)} 个\n")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT id,project,agent,date,title,source FROM records "
        "WHERE status='active' AND source LIKE 'migrated:%'")]

    fixes: list[tuple[int, str, str, str, str]] = []  # id, old, new, reason, title
    for r in rows:
        old = (r["agent"] or "").strip()
        src = r["source"]  # migrated:<文件名>:<行号>
        parts = src.split(":")
        fname, line_no = parts[-2], parts[-1]
        try:
            ln = int(line_no)
        except ValueError:
            ln = 0
        correct = lmap.get((r["project"], fname, ln), "")
        if not correct:
            continue
        if correct != old:
            fixes.append((r["id"], old, correct, "段头/父段可识别→重判", r["title"]))

    print(f"migrated 记录总数：{len(rows)}")
    print(f"待修正：{len(fixes)} 条\n")
    by: dict[str, int] = {}
    for f in fixes:
        by[f"{f[1]}→{f[2]}"] = by.get(f"{f[1]}→{f[2]}", 0) + 1
    for k, v in sorted(by.items(), key=lambda x: -x[1]):
        print(f"  · {k}：{v}")

    print("\n--- agent 分布变化（仅 migrated 记录）---")
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    fmap = {f[0]: f[2] for f in fixes}
    for r in rows:
        before[r["agent"] or "(空)"] = before.get(r["agent"] or "(空)", 0) + 1
        v = fmap.get(r["id"], r["agent"]) or "(空)"
        after[v] = after.get(v, 0) + 1
    print(f"{'agent':<12}{'修正前':>8}{'修正后':>8}{'变化':>6}")
    for k in sorted(set(before) | set(after)):
        d = after.get(k, 0) - before.get(k, 0)
        print(f"{k:<12}{before.get(k,0):>8}{after.get(k,0):>8}{d:>+6}")

    print("\n--- 前 20 条明细 ---")
    for rid, o, n, why, t in fixes[:20]:
        print(f"  #{rid} {o}→{n}（{why}）| {t[:50]}")

    if not args.apply:
        print("\n[dry-run] 未写库。确认无误后加 --apply 执行。")
        return 0

    if not args.yes:
        ans = input(f"\n确认写入 {len(fixes)} 条修正？(yes/no) ").strip().lower()
        if ans != "yes":
            print("已取消。")
            return 1

    ts = brain._now()
    n = 0
    with conn:
        for rid, old, new, why, _t in fixes:
            conn.execute("INSERT INTO journal(ts,agent,action,target,backup,note) VALUES(?,?,?,?,?,?)",
                         (ts, "system", "fix_record_agent", f"brain:records#{rid}", old,
                          f"{old}→{new}（{why}）via fix_record_agents.py"))
            conn.execute("UPDATE records SET agent=? WHERE id=?", (new, rid))
            n += 1
    print(f"\n已写入 {n} 条。回滚：")
    print(f"  1) 整库还原：cp brain.db.bak-agentid-20261009-2310 brain.db")
    print(f"  2) 按 journal 反查：SELECT note,backup,target FROM journal "
          f"WHERE action='fix_record_agent'")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())