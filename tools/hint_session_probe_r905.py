# -*- coding: utf-8 -*-
"""R905：按**单会话**精确判「hint 短提示有没有连发」。

R902 关账 B048 时用的是「单日 + agent」近似（journal 没有 session 列），并留下
精度边界：若日后要精确判单会话连发，需给流水补 session。R905 给 `brain_recall.py`
的 note 补了 `origin=` / `session=`，本探针就是消费它的地方。

**不重开 B048 的结论**：R902 实测 hint 25/140、单日峰值 8 次、不存在连发；
本探针只是把「按天近似」升级成「按会话精确」，让以后真出现连发时能立刻看见。

口径：
  · `mem=[]` = hint 分支（项目近况 / 只给检索词 / 没检索到等 early-return）；
  · 历史行没有 session 字段 → 单列 legacy，不冒充任何会话；
  · 连发 = 同一会话内按时间排序后**连续**出现的 hint 条数（中间夹一次真记忆注入即断）。
"""
import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import _console  # noqa: E402,F401
import brain  # noqa: E402

_SESSION_RE = re.compile(r"\bsession=([^\s·]+)")
_ORIGIN_RE = re.compile(r"\borigin=([A-Za-z_]+)")


def parse_note(note: str) -> dict:
    """从 brain_recall 流水 note 里解析 session / origin / 是不是 hint。"""
    s = str(note or "")
    m = _SESSION_RE.search(s)
    return {
        "session": m.group(1) if m else "",
        "origin": (_ORIGIN_RE.search(s).group(1)
                   if _ORIGIN_RE.search(s) else "unknown"),
        "hint": "mem=[]" in s,
    }


def collect(root: str, days: int = 30, limit: int = None) -> dict:
    """只读 journal，按 session 聚合 hint 次数与最长连发。

    R935：改走 `brain.journal_window`（B108 同款病）。原来是 `journal_list(limit=20000)`，
    被 500 行展示夹子夹住——brain_recall 是高频流水，500 行连一天都盖不住，
    表头却写着「窗口 30 天」。
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") if days > 0 else ""
    rows, capped = brain.journal_window(root, actions=("brain_recall",),
                                       cutoff=cutoff, limit=limit)
    sessions = defaultdict(lambda: {"total": 0, "hint": 0, "streak": 0,
                                    "cur": 0, "origins": defaultdict(int)})
    legacy = 0
    used = 0
    for r in rows:
        p = parse_note(r.get("note"))
        used += 1
        if not p["session"]:
            legacy += 1
            continue
        cell = sessions[p["session"]]
        cell["total"] += 1
        cell["origins"][p["origin"]] += 1
        if p["hint"]:
            cell["hint"] += 1
            cell["cur"] += 1
            cell["streak"] = max(cell["streak"], cell["cur"])
        else:
            cell["cur"] = 0
    for cell in sessions.values():
        cell.pop("cur", None)
        cell["origins"] = dict(cell["origins"])
    return {"sessions": dict(sessions), "legacy": legacy, "used": used,
            "days": days, "cutoff": cutoff, "capped": bool(capped),
            "cap": (brain.JOURNAL_WINDOW_CAP if not limit or int(limit) <= 0
                    else int(limit))}


def render(data: dict) -> str:
    peak_hint = max((c["hint"] for c in data["sessions"].values()), default=0)
    peak_streak = max((c["streak"] for c in data["sessions"].values()), default=0)
    out = [f"hint 会话分布 | 窗口 {data['days']} 天（>= {data['cutoff']}）"
           f" | brain_recall 流水 {data['used']} 行（窗口上限 {data.get('cap', '—')}）", ""]
    if data.get("capped"):
        out.append(f"  ⚠ 窗口内流水已达上限 {data.get('cap')} 行，本次被截断——"
                   f"下面只是可得样本，别按 {data['days']} 天口径下结论。")
        out.append("")
    for sid, c in sorted(data["sessions"].items(),
                         key=lambda kv: -kv[1]["hint"])[:8]:
        origins = "/".join(f"{k}:{v}" for k, v in sorted(c["origins"].items()))
        out.append(f"  {sid[:28]:28s} 注入 {c['total']:3d} · hint {c['hint']:3d}"
                   f" · 最长连发 {c['streak']:2d} · {origins}")
    out.append("")
    out.append(f"单会话 hint 峰值 {peak_hint}，最长连发 {peak_streak}；"
               f"legacy（无 session）{data['legacy']} 行")
    if data["legacy"]:
        out.append("  legacy 行单列，不冒充任何会话（R905 之前写的流水没有 session 字段）。")
    if data.get("capped"):
        out.append("判定：窗口被截断，本轮**不据此**改 B048 状态——先调大 --limit 或收窄 --days 读全再说。")
    elif not data["sessions"]:
        # 记忆 #566：结构性零样本 ≠ 真没攒够。新格式刚上线时全是 legacy 行，
        # 此时说「与 R902 一致」是拿零样本冒充结论，必须明说不下结论。
        out.append("判定：尚无带 session 的样本（全是 legacy）——**不下结论**。")
        out.append("      等真实会话写入若干行后再复跑；R902 的『按天近似』结论仍有效，"
                   "但不是本探针证出来的。")
    elif peak_streak >= 5 or peak_hint >= 10:
        out.append("判定：出现单会话连发，值得复查 B048 是否该重新开账。")
    else:
        out.append("判定：与 R902『不存在单会话连发』的结论一致，B048 保持关账。")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = args.root
    if not root:
        import json
        cfg = Path.home() / ".agenthub" / "config.json"
        root = str(json.loads(cfg.read_text(encoding="utf-8")).get("root") or "")
    text = render(collect(root, args.days))
    print(text)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"已写：{p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
