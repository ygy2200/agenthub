# -*- coding: utf-8 -*-
"""R917：rare_gate 的答案词，在**全库**里到底稀不稀有（只读探针）。

机制根：`_recall_idf(pool_texts, terms)` 的 df 只在 `search_memories(limit=40)`
拿回的候选池里数，且 `df*2>n` 一律压成 0；`_recall_rare_threshold(len(mems))`
也按池大小缩放。R906 全量归因里 12 个期望答案死于这道闸门（最大淘汰原因）。
但「在这 40 条里常见」不等于「在 578 条记忆 + 1880 条记录里常见」——检索本来
就把相关文档聚到池里，池内 df 天然偏高，可能误杀专有词。

本探针用现成的 `brain._token_stats(root, tokens)`（全库活跃行 df，带 memo 缓存）
给每个 rare_gate 答案算一遍全库出现率：
  · `corpus_rare`：全库出现率 <= 阈值 → 池内判定确实误杀，值得做全库视角救援；
  · `globally_common`：全库也常见 → 闸门判对了，放行只会塞噪声，方向证伪。
判据写在前：corpus_rare 占 rare_gate 的比例 >= 50% 才值得进 R918 写代码。

只读：count_hits=False / semantic=False / gate=False，不写库、不写 state。
用法：
    D:/python311/python.exe tools/corpus_idf_probe_r917.py
    D:/python311/python.exe tools/corpus_idf_probe_r917.py --ratio 0.02 --json
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

DEFAULT_RATIO = 0.02   # 全库活跃行里出现率 <=2% 视为稀有


def classify(ratio: float, threshold: float = DEFAULT_RATIO) -> str:
    return "corpus_rare" if ratio <= threshold else "globally_common"


def probe(root: str, cases=None, ratio=DEFAULT_RATIO) -> dict:
    cases = cases if cases is not None else ev.CASES
    rows = []
    for i, (prompt, want, _why) in enumerate(cases, 1):
        accepts = [a for a in ev._accepts(want) if a[0] == "mem"]
        if not accepts:
            continue
        terms = brain.extract_prompt_terms(prompt)
        query = " ".join(t for t, _ in terms)
        try:
            mems = brain.search_memories(root, query, limit=40,
                                         count_hits=False, semantic=False,
                                         gate=False)
        except Exception:  # noqa: BLE001
            mems = []
        pool_texts = [str(r.get("content") or "") + " " + str(r.get("tags") or "")
                      for r in mems]
        pool_raw = brain._recall_idf(pool_texts, terms, weighted=False)
        rare_line = brain._recall_rare_threshold(len(mems))
        try:
            df_corpus, n_total = brain._token_stats(root, [t for t, _ in terms])
        except Exception:  # noqa: BLE001
            df_corpus, n_total = {}, 0
        by_id = {int(r.get("id") or 0): r for r in mems}
        for _kind, mid in accepts:
            r = by_id.get(int(mid))
            if r is None:
                continue
            body = (str(r.get("content") or "") + " " + str(r.get("tags") or "")).lower()
            matched = [t for t, _ in terms if t in body]
            if not matched:
                continue
            best_pool = max((pool_raw.get(t, 0.0) for t in matched), default=0.0)
            if best_pool >= rare_line:
                continue   # 本来就没被 rare 闸门删，不属本探针范围
            ratios = {t: (df_corpus.get(t, 0) / max(1, n_total)) for t in matched}
            best_t = min(ratios, key=lambda t: ratios[t])
            rows.append({"case": i, "id": int(mid), "prompt": prompt,
                         "best_pool_raw": round(best_pool, 3),
                         "rare_line": round(rare_line, 3),
                         "best_term": best_t,
                         "corpus_df": int(df_corpus.get(best_t, 0)),
                         "corpus_total": int(n_total),
                         "corpus_ratio": round(ratios[best_t], 5),
                         "stage": classify(ratios[best_t], ratio)})
    tally = defaultdict(int)
    for r in rows:
        tally[r["stage"]] += 1
    n = len(rows)
    rare_n = tally.get("corpus_rare", 0)
    worth = bool(n) and rare_n / n >= 0.5
    return {"root": root, "cases": len(cases), "ratio_threshold": ratio,
            "rare_gate_answers": n, "tally": dict(tally),
            "corpus_rare_share": round(rare_n / n * 100, 1) if n else 0.0,
            "worth_rescue": worth, "rows": rows}


def render(rep: dict) -> str:
    lines = [f"R917 全库 df 对照探针 | 真库 {rep['root']} | 样本 {rep['cases']} 条",
             f"rare_gate 期望答案 {rep['rare_gate_answers']} 条 | "
             f"全库稀有阈值 出现率<={rep['ratio_threshold']:.0%}",
             f"分类：corpus_rare {rep['tally'].get('corpus_rare', 0)} · "
             f"globally_common {rep['tally'].get('globally_common', 0)} · "
             f"占比 {rep['corpus_rare_share']:.1f}%", ""]
    for r in rep["rows"][:20]:
        lines.append(f"  #{r['case']:02d} mem#{r['id']:<4d} {r['stage']:<16s} "
                     f"池内raw={r['best_pool_raw']:.2f}/{r['rare_line']:.2f}  "
                     f"全库词={r['best_term']!r} df={r['corpus_df']}/{r['corpus_total']}"
                     f" ({r['corpus_ratio']:.3%})")
    lines.append("")
    lines.append("判据：corpus_rare 占 rare_gate ≥50% 才值得进 R918 做全库视角救援；"
                 "否则方向证伪——闸门判对了，放行只会塞噪声。")
    lines.append(f"结论：{'值得做（R918 实施 + A/B）' if rep['worth_rescue'] else '不值得做，方向证伪'}。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--ratio", type=float, default=DEFAULT_RATIO)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = args.root or str(ev._brain_root())
    rep = probe(root, ratio=args.ratio)
    text = (json.dumps(rep, ensure_ascii=False, indent=1) if args.json
            else render(rep) + "\n")
    print(text, end="")
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
