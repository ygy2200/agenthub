# -*- coding: utf-8 -*-
"""R1067：零候选样本的逐条归因（只读）。

recall_eval 里 `gap` 类 = 「期望答案所在的通道，连一条候选都没进 top3」。R904 已经
把它从「难度分母」里摘出来（避免用调权重去救召回上限问题），但**没人回答过**这几条
到底卡在哪一步：答案不在池里？在池里但被 hit 闸门删？还是有词面命中、死于稀有词线？

本量具对每条 gap 样本逐步打卡，四态必居其一：
  NOT_IN_POOL   词法池里根本没有这条 → 召回上限问题（要么换检索式，要么靠语义补池）
  POOL_NO_SIM   在池里，但语义相似度低于准入线 → 语义路也没证据，属真·词汇鸿沟
  HIT_GATE      在池里、语义够近，但词面命中 < need → 应被 R1063 准入档捞起（若没捞，是缺陷）
  RARE_GATE     过了 hit 闸门但被稀有词线判弱 → R900/R907 救援的辖区

用法：
    D:/python311/python.exe tools/gap_attribution_r1067.py
    D:/python311/python.exe tools/gap_attribution_r1067.py --out ../output/xxx.txt
"""
import argparse
import io
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402


def _row_of(ids, rows):
    want = {int(v) for _k, v in ids}
    return [r for r in rows if int(r.get("id") or 0) in want]


def attribute(root: str, prompt: str, accepts) -> dict:
    terms = brain.extract_prompt_terms(prompt)
    q = " ".join(t for t, _ in terms)
    pool = brain.search_memories(root, q, limit=brain.RECALL_POOL_LIMIT,
                                 count_hits=False, semantic=False, gate=False)
    pool_ids = {int(r.get("id") or 0) for r in pool}
    targets = {int(v) for _k, v in accepts}
    hits = sorted(targets & pool_ids)

    # 语义分：只为目标行查一次（limit 拉满，别用 40 的口径把答案挡在外面）
    sim_map = {}
    try:
        for eid, _rrf, row in brain._semantic_rank(root, prompt, "memories", limit=200):
            sim_map[int(row.get("id") or eid)] = float(row.get("_semantic_sim") or 0.0)
    except Exception:  # noqa: BLE001 可选能力：拿不到分就按 0 记
        sim_map = {}

    body_by_id = {int(r.get("id") or 0): (str(r.get("content") or "") + " "
                                          + str(r.get("tags") or "")) for r in pool}
    detail = []
    for mid in sorted(targets):
        body = body_by_id.get(mid, "")
        hit = brain._recall_hit_score(terms, body) if body else -1
        detail.append({"id": mid, "in_pool": mid in pool_ids,
                       "hit": hit, "sim": round(sim_map.get(mid, 0.0), 3)})

    if not hits:
        state = "NOT_IN_POOL"
    elif any(d["hit"] >= 2 and d["in_pool"] for d in detail):
        state = "RARE_GATE"
    elif any(d["in_pool"] and d["sim"] >= brain.RECALL_SEM_ADMIT_MIN
             for d in detail):
        state = "HIT_GATE"
    else:
        state = "POOL_NO_SIM"
    return {"prompt": prompt, "terms": [t for t, _ in terms], "state": state,
            "pool": len(pool), "detail": detail}


def run(root: str, out: str = "") -> int:
    buf = io.StringIO()
    buf.write(f"R1067 零候选归因 | 真库 {root} | 准入线 {brain.RECALL_SEM_ADMIT_MIN}\n")
    buf.write(ev.env_guard_line(root) + "\n\n")
    cases = ev.CASES
    # 先跑一遍 recall_eval 的判定，只挑 gap 样本（量具口径与线上一致）
    import tempfile
    state = Path(tempfile.mkdtemp(prefix="gap_attr_")) / "state.json"
    gap_rows = []
    for i, (prompt, want, why) in enumerate(cases, 1):
        accepts = ev._accepts(want)
        if not accepts:
            continue
        res = brain.prompt_recall(root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                                  session=f"gap-{i}", state_path=str(state),
                                  update_state=False, origin="eval")
        cls, _s, _l = ev._classify(prompt, accepts, res, root)
        if cls == "gap":
            gap_rows.append((i, prompt, accepts, why, dict(res.get("dropped") or {})))
    buf.write(f"gap 样本 {len(gap_rows)} 条\n\n")
    for i, prompt, accepts, why, drop in gap_rows:
        rep = attribute(root, prompt, accepts)
        buf.write(f"[{i:02d}] {prompt!r}\n")
        buf.write(f"     类型：{why}\n     terms={rep['terms']} · 池={rep['pool']} 条"
                  f" · 判定={rep['state']} · dropped={drop}\n")
        for d in rep["detail"]:
            buf.write(f"     - mem#{d['id']}: in_pool={d['in_pool']} "
                      f"hit={d['hit']} sim={d['sim']:.3f}\n")
        buf.write("\n")
    text = buf.getvalue()
    print(text)
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"已写：{p}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = args.root or ev._brain_root()
    return run(root, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
