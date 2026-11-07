# -*- coding: utf-8 -*-
"""R947（B113）：「按 kind 给默认有效期」的只读 A/B —— 先量误伤，再谈加不加。

背景（R942 已更正定性，别再改检索侧）：`valid_until` 的过滤机制**完整且正确**
（brain.py:5944/5954 检索 SQL、7584/7605 硬过期优先、7668/7680 治理跳过、
6125 写入侧、14245 展示），真问题只是 591 条里只有 6 条设了有效期、且全是
2027 年未来日期、过期 0 条——这套逻辑在真库里从没机会触发。

B113 verify 原话：「若按 kind 给 90/180 天默认有效期，30 天内有多少条当前被推送的
记忆会退出、其中多少仍有用；零收益或误伤则维持现状」。本轮只做这个只读 A/B，
**不改任何代码、不写库、不动检索侧**。

判据（写在前）：任一档位下「会被判过期」的记忆里，只要存在近 30 天仍被注入/
被采纳过的条目，就算误伤 → 维持现状，不建议加默认有效期。理由：定时过期是
不可逆的召回损失，而收益（少推几条老记忆）在当前 564 条库上无法证明。

用法：
    D:/python311/python.exe tools/valid_until_default_ab_r947.py
    D:/python311/python.exe tools/valid_until_default_ab_r947.py --out ../output/_r947_valid_until_ab.log
"""
import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  R940：tools/ 新脚本必须装控制台 UTF-8 守卫
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

# 「按 kind 分档」与「全库统一」两族都测，避免只测一个拍脑袋的数。
KIND_POLICY = {"lesson": 180, "fact": 365, "project": 180,
               "preference": 9999, "note": 90}
FLAT_POLICIES = [("flat90", 90), ("flat180", 180), ("flat365", 365),
                 ("kind_policy", None)]
RECENT_DAYS = 30


def age_days(created: str, today: dt.date) -> int | None:
    """纯函数：入库日期到今天的天数；格式不对返回 None（不猜）。"""
    s = str(created or "")[:10]
    try:
        return (today - dt.date.fromisoformat(s)).days
    except (ValueError, TypeError):
        return None


def expire_stats(rows: list, policy: dict | int | None, today: dt.date,
                 recent_days: int = RECENT_DAYS) -> dict:
    """纯函数：模拟一档默认有效期，数「会退出多少 / 其中多少仍在用」。

    `policy=None` 表示不设期限（对照组）；dict 按 kind 取天数；int 为全库统一。
    「仍在用」= 近 N 天被注入过（last_inject）或被采纳过（adopt_count>0）——
    采纳次数没有时间戳，用「曾采纳」作保守上界，宁可高估误伤也不低估。
    """
    cutoff = (today - dt.timedelta(days=recent_days)).isoformat()
    out = {"total": len(rows), "expired": 0, "still_used": 0,
           "used_but_fresh": 0, "by_kind": {}}
    for r in rows:
        if str(r.get("valid_until") or "").strip():
            continue  # 已显式设有效期的，默认策略管不着
        days = age_days(r.get("created"), today)
        if days is None:
            continue
        limit = policy if isinstance(policy, int) else (
            (policy or {}).get(str(r.get("kind") or "note"), 0) if policy else 0)
        if not limit or days <= limit:
            continue
        out["expired"] += 1
        k = str(r.get("kind") or "note")
        out["by_kind"][k] = out["by_kind"].get(k, 0) + 1
        injected_recent = str(r.get("last_inject") or "")[:10] >= cutoff
        adopted = int(r.get("adopt_count") or 0) > 0
        if injected_recent or adopted:
            out["still_used"] += 1
    out["harm_rate"] = (out["still_used"] / out["expired"]) if out["expired"] else None
    return out


def _load(root: str) -> list:
    uri = brain.db_path(root).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT id, kind, created, valid_until, last_inject, inject_count, "
            "adopt_count, use_count, pinned, confidence, substr(content,1,48) AS preview "
            "FROM memories WHERE status='active' AND superseded_by=0")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = args.root or ev._brain_root()
    stream = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout

    def say(s=""):
        print(s, file=stream)

    fp = ev.env_fingerprint(root)
    say(f"环境指纹 env={ev.env_key(fp)} · db={ev.db_key(fp)}"
        f"（memories {fp['memories']} / records {fp['records']}）")
    say(f"判据（写在前）：任一档位 still_used > 0 即判误伤 → 维持现状，不加默认有效期。")
    say("只读：sqlite mode=ro，全程 SELECT，不写库、不改检索侧（R942 已核实过滤正确）。")

    rows = _load(root)
    today = dt.date.today()
    with_vu = sum(1 for r in rows if str(r.get("valid_until") or "").strip())
    ages = [a for a in (age_days(r.get("created"), today) for r in rows) if a is not None]
    ages.sort()
    say(f"\nactive & 未被取代 {len(rows)} 条 · 已显式设 valid_until {with_vu} 条 · "
        f"年龄 p50={ages[len(ages)//2] if ages else '-'} 天 / "
        f"p90={ages[int(len(ages)*0.9)] if ages else '-'} 天 / "
        f"最大 {ages[-1] if ages else '-'} 天")

    say("\n档位          会退出  其中仍在用  误伤率   按 kind 分布")
    results = []
    for name, pol in [("现状(不设)", None)] + FLAT_POLICIES:
        p = KIND_POLICY if pol is None and name == "kind_policy" else (
            pol if pol is not None else None)
        s = expire_stats(rows, p, today)
        hr = "—" if s["harm_rate"] is None else f"{s['harm_rate']*100:.1f}%"
        dist = "、".join(f"{k}:{v}" for k, v in sorted(s["by_kind"].items())) or "无"
        say(f"{name:<14} {s['expired']:>5}  {s['still_used']:>9}  {hr:>7}   {dist}")
        results.append((name, s))

    say("\n逐条点名「会被判过期、但近 30 天仍在用」的记忆（误伤证据）：")
    cutoff = (today - dt.timedelta(days=RECENT_DAYS)).isoformat()
    shown = 0
    for r in rows:
        if str(r.get("valid_until") or "").strip():
            continue
        a = age_days(r.get("created"), today)
        if a is None or a <= 90:
            continue
        if str(r.get("last_inject") or "")[:10] >= cutoff or int(r.get("adopt_count") or 0) > 0:
            say(f"  #{r['id']} kind={r['kind']} 年龄={a}天 "
                f"last_inject={str(r.get('last_inject') or '-')[:10]} "
                f"inject={r.get('inject_count')} adopt={r.get('adopt_count')} "
                f"pinned={r.get('pinned')} :: {' '.join(str(r.get('preview') or '').split())[:40]}")
            shown += 1
            if shown >= 12:
                say("  …（只列前 12 条）")
                break
    if not shown:
        say("  无。")

    bad = [(n, s) for n, s in results if s["still_used"] > 0]
    zero_expiry = all(s["expired"] == 0 for n, s in results if n != "现状(不设)")
    say("\n结论：")
    if zero_expiry:
        say(f"  所有档位会退出 **0 条**——真库最老的 active 记忆只有 "
            f"{ages[-1] if ages else 0} 天，连最短的 90 天档都够不着。")
        say("  这不是「没误伤」，是**分母为 0、方向当前不可验证**（记忆 #566 口径："
            "量不出不等于没问题，不许把零当成结论）。")
        say("  → B113 关账为「只读测量，前提不成立」：不加默认有效期、检索侧一行不动。"
            "复查条件写死：真库最老记忆 > 90 天后重跑本量具（届时 flat90 才有分母）。")
        say("  同一条也约束 R944 登记的 B117（衰减 / 分层合并）：库龄不足时任何遗忘策略"
            "都量不出收益，不得拿「业界都这么干」当立项理由。")
    elif bad:
        say(f"  {len(bad)} 个档位会把仍在用的记忆判过期（最多的一次 "
            f"{max(s['still_used'] for _, s in bad)} 条）。默认有效期是**不可逆的召回损失**，"
            "而收益（少推几条老记忆）在这个库上没有被证明。")
        say("  → **维持现状**：不加默认有效期、检索侧一行不动。B113 关账为「只读测量，"
            "方向证伪」。真问题是写入侧几乎不设有效期，属引导问题（可另立轮次在"
            " hub_memory_write 的提示里建议显式设 valid_until），不是过滤机制问题。")
    else:
        say("  所有档位的会退出条目里，近 30 天仍在用的为 0 —— 没测出误伤。")
        say("  → 仍**不改代码**：本轮是只读 A/B，是否引入默认有效期属产品语义变更，"
            "按用户偏好需拍板；证据先落盘，登记为待确认。")
    if args.out:
        stream.close()
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
