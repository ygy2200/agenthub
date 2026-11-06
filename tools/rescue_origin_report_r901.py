# -*- coding: utf-8 -*-
"""R901：语义救援在「真实会话」里到底触发几次——按 origin 分档读流水。

背景：R900 的 A/B 是在**已预热的真库**上跑的（origin=eval），胜出档 +9.6pp。
但台账遗留写了三条待确认：语义路冷启动约 1.0s、钩子路径只在预热后生效、
真实会话命中率从未量过。默认档一上线，如果没有观测出口，就没人能回答
「线上到底有没有触发」，下一轮又会退回靠猜。

B077 之前的半解：`brain_recall_semantic_bridge` 的 note 里已写 `origin=`，但字段级
单列一直没做。这里不新建表、不改 schema（零迁移风险），按 note 解析即可，并且
**同时覆盖两类救援**：R861 词法零候选的语义桥 + R901 闸门救援。

口径硬约束（记忆 #566：两类「等不到样本」不是一回事）：
  · live=0 时必须明说「真实会话尚无样本，不下结论」，不许把 probe/eval 的触发
    数当成线上收益报出去；
  · 评测/探针装置写的行数单列，别混进 live。
"""
import argparse
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import _console  # noqa: E402,F401
import brain  # noqa: E402

ACTIONS = ("brain_recall_semantic_bridge", "brain_recall_sem_rescue")
ORIGINS = ("live", "probe", "eval")
_ORIGIN_RE = re.compile(r"\borigin=([A-Za-z_]+)")


def _origin_of(note: str) -> str:
    m = _ORIGIN_RE.search(str(note or ""))
    return m.group(1) if m and m.group(1) in ORIGINS else "unknown"


def collect(root: str, days: int = 30, limit: int = None,
            switch_ts: str = "") -> dict:
    """只读 journal，按 (action, origin) 出计数与最近一次时间。

    R933 补「实得最早一行」，R935 把口径本身做实：改用 `brain.journal_window`
    按 action+窗口读（B108）。此前走 `journal_list`，limit 被夹在 500——请求 30 天
    实际只扫到最近两天，live 计数是「可得样本」而不是「窗口样本」，报表只能免责声明。
    现在窗口读全，`capped` 只在真撞到窗口上限时才真，`covered` 就是它的反面。
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") if days > 0 else ""
    rows, capped = brain.journal_window(root, actions=ACTIONS, cutoff=cutoff, limit=limit)
    counts = {a: Counter() for a in ACTIONS}
    last = {a: "" for a in ACTIONS}
    all_ts = [str(r.get("ts") or "") for r in rows if r.get("ts")]
    live_ts = []
    for r in rows:
        action = str(r.get("action") or "")
        if action not in counts:
            continue
        full = str(r.get("ts") or "")
        ts = full[:10]
        o = _origin_of(r.get("note"))
        counts[action][o] += 1
        if o == "live":
            live_ts.append(full)
        if ts > last[action]:
            last[action] = ts
    cap = brain.JOURNAL_WINDOW_CAP if not limit or int(limit) <= 0 else int(limit)
    return {"counts": counts, "last": last, "total_rows": len(rows),
            "days": days, "cutoff": cutoff, "earliest": min(all_ts) if all_ts else "",
            "capped": bool(capped), "covered": not capped, "cap": cap,
            "live_ts": live_ts, "switch_ts": switch_ts,
            "live_after_switch": len([t for t in live_ts if switch_ts and t >= switch_ts])}


def render(data: dict, switch_ts: str = "") -> str:
    out = [f"语义救援触发分档 | 窗口 {data['days']} 天（>= {data['cutoff'] or '不限'}）"
           f" | 按窗口聚合读 {data['total_rows']} 行（窗口上限 {data.get('cap', '—')}）", ""]
    if data.get("earliest"):
        out.append(f"实得行最早 {data['earliest']}")
    if data.get("capped"):
        out.append(f"  ⚠ 窗口内流水已达上限 {data.get('cap')} 行，本次被截断——下面分档是**可得样本**，"
                   f"不是整窗口统计；要读全就调大 --limit 或收窄 --days。")
    out.append("")
    live_total = 0
    for a in ACTIONS:
        c = data["counts"][a]
        line = (f"{a:34s} live={c['live']:4d}  probe={c['probe']:4d}  "
                f"eval={c['eval']:4d}  unknown={c['unknown']:3d}  "
                f"最近={data['last'][a] or '—'}")
        out.append(line)
        live_total += c["live"]
    out.append("")
    st = switch_ts or data.get("switch_ts") or ""
    if st:
        pre = [t for t in data.get("live_ts") or [] if t < st]
        out.append(f"生产开关切换：{st}")
        if pre:
            out.append(f"  ⚠ {len(pre)} 笔 live 行早于生产开关（{', '.join(pre[:5])}）"
                       f"——不能当成开关生效后的收益；开关后 live={data.get('live_after_switch', 0)} 笔。")
        out.append("")
    if live_total == 0:
        out.append("判定：真实会话（origin=live）触发 0 次 —— 样本不足，**不下结论**。")
        out.append("      probe/eval 的触发数只证明装置在跑，不能当成线上收益。")
        out.append("      下一步：默认档上线后攒够 ≥30 笔 live 再复评 R900 的 +9.6pp。")
    else:
        out.append(f"判定：真实会话累计触发 {live_total} 次；"
                   "可结合 recall_adherence_report 看这些注入是否被采纳。")
    return "\n".join(out) + "\n"


def _prod_switch_ts() -> str:
    """生产 MCP 配置的落盘时刻 ≈ R914 语义开关的实际生效时间；读不到就留空，不猜。"""
    try:
        p = Path.home() / ".workbuddy" / "mcp.json"
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
    except OSError:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=0,
                    help="窗口行数上限，0=用 brain.JOURNAL_WINDOW_CAP（不是 journal_list 的 500）")
    ap.add_argument("--switch-ts", default="",
                    help="生产语义开关生效时间（默认取 ~/.workbuddy/mcp.json 的修改时间）")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = args.root
    if not root:
        import json
        cfg = Path.home() / ".agenthub" / "config.json"
        root = str(json.loads(cfg.read_text(encoding="utf-8")).get("root") or "")
    switch_ts = args.switch_ts or _prod_switch_ts()
    text = render(collect(root, args.days, args.limit or None, switch_ts=switch_ts))
    print(text)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"已写：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
