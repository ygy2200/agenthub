# -*- coding: utf-8 -*-
"""R1083：记录路 11 条失败样本逐条归因（只读），并更正 R1081 的一处措辞。

R1081 台账写「记录路只有纯词法」——**池层这句不准确**：`search_records` 走
`_records_search_fused`，实测 `_RRF_ENABLED=True` / `_RRF_W_SEMANTIC_RECORDS=1.5`，
候选池本来就是 LIKE + BM25 + 语义第三路 + 图第四路融合，语义独有行也能进池
（brain.py:2480-2522）。真正只有词法的是 `prompt_recall` 的**记录打分/闸门层**
（brain.py:10493-10539）：`hit < eff_need` 硬闸、`_recall_rare_threshold` 稀有词线，
全程不读 `_semantic_sim`。

由此产生一个可证伪的假设，且与记忆路 R1062→R1063 的历史完全同构：
  语义第三路把「词面零命中但语义够近」的记录补进了池，
  记录路却用纯词法 hit 闸门把它们整批删掉 —— 语义路等于白接。
R1063 为记忆路修过一次（语义准入只补空位），那套修法**从未移植到记录路**。

四态归因（对每条失败样本的目标记录）：
  NOT_IN_POOL    词法/融合池里根本没有这条 → 池层问题（附语义路能否找到）
  SEM_KILLED     在池里、靠语义路补进来的（channels 只有 semantic）、hit<need 被删
  HIT_GATE       在池里、有词面命中但不足 need
  RARE_GATE      过了 hit 闸门，被稀有词线判弱
  CAP_OR_RANK    strong 里但没挤进 RECALL_RECORD_CAP=2

用法：
    D:/python311/python.exe tools/records_gap_attribution_r1083.py --out ../output/xxx.txt
"""
import argparse
import io
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import _abroot  # noqa: E402
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402
import records_recall_eval as rce  # noqa: E402


def _sem_map(root, prompt, limit=200):
    try:
        return {int(row.get("id") or eid): float(row.get("_semantic_sim") or 0.0)
                for eid, _s, row in brain._semantic_rank(root, prompt, "records", limit=limit)}
    except Exception:  # noqa: BLE001 可选能力
        return {}


def attribute(root, prompt, target, terms):
    query = " ".join(t for t, _ in terms)
    pool = brain.search_records(root, query, limit=30)
    by_id = {int(r.get("id") or 0): r for r in pool}
    row = by_id.get(int(target))
    sem = _sem_map(root, prompt)
    rep = {"prompt": prompt, "target": int(target), "pool": len(pool),
           "in_pool": row is not None, "terms": [t for t, _ in terms],
           "sem": round(sem.get(int(target), 0.0), 3),
           "sem_rank": (sorted(sem, key=lambda k: -sem[k]).index(int(target)) + 1
                        if int(target) in sem else -1),
           "hit": -1, "need": (2 if len(terms) >= 2 else 1),
           "channels": {}, "state": ""}
    if row is None:
        rep["state"] = "NOT_IN_POOL"
        return rep
    body = str(row.get("title") or "") + " " + str(row.get("content") or "")
    hit = brain._recall_hit_score(terms, body)
    rep["hit"] = hit
    rep["channels"] = dict(row.get("_channels") or {})
    rep["row_sim"] = round(float(row.get("_semantic_sim") or 0.0), 3)
    if hit < rep["need"]:
        only_sem = bool(rep["channels"]) and set(rep["channels"]) == {"semantic"}
        rep["state"] = "SEM_KILLED" if only_sem else "HIT_GATE"
        return rep
    low = body.lower()
    texts = [str(r.get("title") or "") + " " + str(r.get("content") or "") for r in pool]
    idf_raw = brain._recall_idf(texts, terms, weighted=False)
    best = max((idf_raw.get(t, 0.0) for t, _ in terms if t in low), default=0.0)
    rep["best"] = round(best, 3)
    rep["rare_line"] = round(brain._recall_rare_threshold(len(pool)), 3)
    if best < rep["rare_line"]:
        rep["state"] = "RARE_GATE"
    else:
        rep["state"] = "CAP_OR_RANK"
    return rep


def run(root: str = "", out: str = "") -> int:
    root = root or ev._brain_root()
    buf = io.StringIO()
    buf.write(f"R1083 记录路失败样本归因 | 真库 {root} | 样本 {len(rce.CASES_REC)} 条\n")
    buf.write(ev.env_guard_line(root) + "\n")
    buf.write(f"池层实测：_RRF_ENABLED={brain._RRF_ENABLED} · "
              f"_RRF_W_SEMANTIC_RECORDS={brain._RRF_W_SEMANTIC_RECORDS} · "
              f"RECALL_RECORD_CAP={brain.RECALL_RECORD_CAP}\n\n")

    state = Path(tempfile.mkdtemp(prefix="rec_gap_")) / "state.json"
    fails, reports = [], []
    snap = _abroot.snapshot_root(root)
    try:
        for i, (prompt, target, why) in enumerate(rce.CASES_REC, 1):
            res = brain.prompt_recall(
                snap, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"recgap-{i}", state_path=str(state),
                update_state=False, origin="eval")
            cls, _span, _leak = rce._classify(snap, prompt, target, res)
            if cls in ("hit", "leak"):
                continue
            terms = brain.extract_prompt_terms(prompt)
            # R1085 起 target 可为合格集；归因只取主 id（最小者）做定位。
            rep = attribute(snap, prompt, min(rce.accepts_of(target)), terms)
            rep.update(i=i, cls=cls, why=why,
                       got=[int(v) for v in (res.get("record_ids") or [])[:2]])
            reports.append(rep)
            fails.append(i)
    finally:
        _abroot.drop_root(snap)

    tally = defaultdict(int)
    for r in reports:
        tally[r["state"]] += 1
    buf.write(f"失败样本 {len(reports)} 条（编号 {fails}）\n")
    buf.write("归因汇总：" + " · ".join(f"{k} {v}" for k, v in sorted(tally.items())) + "\n\n")
    for r in reports:
        buf.write(f"[{r['i']:02d}] {r['cls']:<4} -> {r['state']:<12} "
                  f"want=rec#{r['target']} got={r['got']}\n"
                  f"      {r['prompt']!r}\n"
                  f"      {r['why']}\n"
                  f"      terms={r['terms']} · 池={r['pool']} · hit={r['hit']}/need={r['need']}"
                  f" · channels={r['channels']} · 语义分={r['sem']}(rank {r['sem_rank']})"
                  f" · best={r.get('best', '-')}/rare={r.get('rare_line', '-')}\n\n")
    sem_killed = [r for r in reports if r["state"] == "SEM_KILLED"]
    not_in = [r for r in reports if r["state"] == "NOT_IN_POOL"]
    buf.write("判据（写在前，供 R1084 用）：\n")
    buf.write(f"  · SEM_KILLED {len(sem_killed)} 条 = 语义路已把答案送进池、"
              "却被词法硬闸删掉；若 ≥2 条，移植 R1063 语义准入到记录路才值得做 A/B。\n")
    buf.write(f"  · NOT_IN_POOL {len(not_in)} 条中语义 rank≤10 的有 "
              f"{sum(1 for r in not_in if 0 < r['sem_rank'] <= 10)} 条，"
              "这才是需要语义补池的部分（与 SEM_KILLED 是两回事，别混）。\n")
    buf.write("  · 其余 HIT_GATE / RARE_GATE / CAP_OR_RANK 属词法排序辖区，"
              "语义准入救不了，别拿它当收益。\n")
    text = buf.getvalue()
    print(text)
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        Path(str(p) + ".json").write_text(json.dumps(
            {"tally": dict(tally), "reports": reports}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"已写：{p} (+ .json)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    return run(args.root, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
