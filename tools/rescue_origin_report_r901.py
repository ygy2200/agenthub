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


def collect(root: str, days: int = 30, limit: int = 20000) -> dict:
    """只读 journal，按 (action, origin) 出计数与最近一次时间。"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = brain.journal_list(root, limit=limit) or []
    counts = {a: Counter() for a in ACTIONS}
    last = {a: "" for a in ACTIONS}
    for r in rows:
        action = str(r.get("action") or "")
        if action not in counts:
            continue
        ts = str(r.get("ts") or "")[:10]
        if days > 0 and ts and ts < cutoff:
            continue
        o = _origin_of(r.get("note"))
        counts[action][o] += 1
        if ts > last[action]:
            last[action] = ts
    return {"counts": counts, "last": last, "total_rows": len(rows),
            "days": days, "cutoff": cutoff}


def render(data: dict) -> str:
    out = [f"语义救援触发分档 | 窗口 {data['days']} 天（>= {data['cutoff']}）"
           f" | journal 扫描 {data['total_rows']} 行", ""]
    live_total = 0
    for a in ACTIONS:
        c = data["counts"][a]
        line = (f"{a:34s} live={c['live']:4d}  probe={c['probe']:4d}  "
                f"eval={c['eval']:4d}  unknown={c['unknown']:3d}  "
                f"最近={data['last'][a] or '—'}")
        out.append(line)
        live_total += c["live"]
    out.append("")
    if live_total == 0:
        out.append("判定：真实会话（origin=live）触发 0 次 —— 样本不足，**不下结论**。")
        out.append("      probe/eval 的触发数只证明装置在跑，不能当成线上收益。")
        out.append("      下一步：默认档上线后攒够 ≥30 笔 live 再复评 R900 的 +9.6pp。")
    else:
        out.append(f"判定：真实会话累计触发 {live_total} 次；"
                   "可结合 recall_adherence_report 看这些注入是否被采纳。")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=20000)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = args.root
    if not root:
        import json
        cfg = Path.home() / ".agenthub" / "config.json"
        root = str(json.loads(cfg.read_text(encoding="utf-8")).get("root") or "")
    text = render(collect(root, args.days, args.limit))
    print(text)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"已写：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
