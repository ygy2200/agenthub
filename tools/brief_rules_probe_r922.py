# -*- coding: utf-8 -*-
"""R922：简报通道（必做规则）到底能不能测采纳——只读探针。

背景：`recall_adherence` 只记 `prompt_recall`（UserPromptSubmit 逐句回忆）的注入。
而真正改变 agent 行为的那一路是 **SessionStart 简报**：R882 的共享规则簿把
`tags=必做` 的记忆作为「必须照做」开局塞给每个 agent。这一路在采纳账上完全空白，
于是 R879 立的「required vs supportive 谁更有用」永远攒不出 required 样本
（2026-11-06 实测：判过 182 笔里 required 0 笔）——不是这条档位没用，是**没测**。

想补这一路，先得回答一个前提问题：**必做规则的文本抽得出指纹吗？**
采纳判定的唯一证据是「记忆里的招牌 token 后来出现在 agent 的产出里」
（R827 的口径，外部依据是 Grimoire 实测「让模型写引用标签没用」）。规则文本
多是「凡动 X 一律走 Y 技能」这类自然语言，若招牌抽不出来，接了通道也只是
把分母撑大一堆 `fp=[]`，测了等于没测。

判据（写在前，不许事后放宽）：
  · `measurable`：可指纹规则占比 ≥ 50% 且近 7 天简报送达 ≥ 20 次
    → 下一轮直接接通道（origin=brief 计入生产分母）；
  · `partial`：20% ≤ 占比 < 50% → 接，但报表必须单列「判不了」，不许并进分母；
  · `not_measurable`：占比 < 20% → 这条通道作废，改用别的信号（标待确认）。

只读：不写 recall_adherence、不落 journal、不改任何记忆。
用法：
    D:/python311/python.exe tools/brief_rules_probe_r922.py
    D:/python311/python.exe tools/brief_rules_probe_r922.py --json --out <path>
"""
import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402

RULES_LIMIT = 40          # 与 session_brief 的 LIMIT 一致
FP_RATIO_OK = 0.5
FP_RATIO_PARTIAL = 0.2
DELIVERIES_MIN = 20


def rule_rows(root: str) -> list:
    """照抄 session_brief 的规则取数（同一个判据，不另设口径）。"""
    with brain.db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id,kind,content,tags,updated FROM memories WHERE " + brain._live_sql()
            + " AND tags LIKE '%必做%' ORDER BY pinned DESC, updated DESC, id DESC"
            + f" LIMIT {RULES_LIMIT}")]
    return [r for r in rows
            if "必做" in brain._tag_tokens(r.get("tags")) and not brain._is_self_doc(r)]


def fingerprints_of(row) -> list:
    return brain.recall_fingerprints(str(row.get("content") or "") + " "
                                     + str(row.get("tags") or ""),
                                     pool_texts=None, prompt="")


def deliveries(root: str, days: int = 7) -> dict:
    """简报实际送达：journal 里 action=brain_brief 的行（钩子/MCP/initialize 分列）。"""
    since = brain._days_ago(days)
    out = {"total": 0, "by_day": {}, "by_target": {}, "agents": []}
    with brain.db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT ts,agent,target FROM journal WHERE action='brain_brief' AND ts>=?"
            " ORDER BY ts", (since,))]
    out["total"] = len(rows)
    for r in rows:
        day = (r.get("ts") or "")[:10]
        out["by_day"][day] = out["by_day"].get(day, 0) + 1
        tgt = r.get("target") or "-"
        out["by_target"][tgt] = out["by_target"].get(tgt, 0) + 1
    out["agents"] = sorted({(r.get("agent") or "") for r in rows if r.get("agent")})
    return out


def classify(fp_ratio: float, deliveries_n: int) -> str:
    if fp_ratio >= FP_RATIO_OK and deliveries_n >= DELIVERIES_MIN:
        return "measurable"
    if fp_ratio >= FP_RATIO_PARTIAL:
        return "partial"
    return "not_measurable"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="必做规则指纹可测性探针（只读）")
    ap.add_argument("--root", default="", help="大脑根目录，默认读 config.json")
    ap.add_argument("--days", type=int, default=7, help="送达统计窗口（默认 7 天）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", type=Path, help="JSON 落盘路径")
    args = ap.parse_args(argv)
    root = (args.root or (brain.core.load_config() or {}).get("root", "") or "").strip()
    if not root or not Path(root).is_dir():
        print(f"大脑根目录不可用：{root!r}")
        return 2
    rows = rule_rows(root)
    detail = []
    fped = 0
    rule_chars = 0
    for r in rows:
        fp = fingerprints_of(r)
        if fp:
            fped += 1
        body = str(r.get("content") or "")
        rule_chars += len(body)
        detail.append({"id": r["id"], "fp_n": len(fp),
                       "tokens": fp[:3], "chars": len(body),
                       "title": body.strip().splitlines()[0][:40] if body.strip() else ""})
    n = len(rows)
    ratio = (fped / n) if n else 0.0
    dv = deliveries(root, args.days)
    verdict = classify(ratio, dv["total"])
    payload = {"root": root, "rules": n, "fingerprintable": fped, "fp_ratio": round(ratio, 3),
               "deliveries": dv, "verdict": verdict,
               "thresholds": {"measurable_fp": FP_RATIO_OK, "partial_fp": FP_RATIO_PARTIAL,
                              "deliveries_min": DELIVERIES_MIN},
               "rule_chars": rule_chars, "brief_budget": brain.BRIEF_MAX_CHARS,
               "detail": detail}
    if args.json or args.out:
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        if args.out:
            args.out.write_text(text, encoding="utf-8")
            print(f"已写 {args.out}")
        if args.json:
            print(text)
    print(f"必做规则 {n} 条 · 可指纹 {fped} 条（{ratio * 100:.1f}%）"
          f" · 近 {args.days} 天简报送达 {dv['total']} 次")
    print("送达分列：", " · ".join(f"{k}={v}" for k, v in sorted(dv["by_target"].items())) or "无")
    # R924 更正：这一行以前写成「规则占了 108%」，把「原文字数总和」当成了「简报里占的字数」——
    # 渲染时每条走 `_brief_rule_line(content, 120)`，实占 950 字（见 tools/brief_layout_probe_r924.py）。
    # 口径写清楚，别再拿它当分段预算的依据。
    print("规则原文字数合计", rule_chars, "（不是渲染量；简报实占见 brief_layout_probe_r924）")
    for d in detail:
        print(f"  #{d['id']:<4} fp={d['fp_n']} {d['tokens']}  {d['title']}")
    print(f"判定：{verdict}（判据写在前：可指纹≥{FP_RATIO_OK:.0%} 且送达≥{DELIVERIES_MIN} 才接通道）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
