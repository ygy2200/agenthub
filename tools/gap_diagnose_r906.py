# -*- coding: utf-8 -*-
"""R906（B103）：把「两路零候选」拆成可归因的流水线阶段。

为什么要有这个文件：R904 报出 3 条 gap（#15/#24/#26），报表只写「两路零候选」；
`brain.prompt_recall` 的 `dropped` 是**全池计数**（这一轮共删了多少 hit/rare/immune/cap），
回答不了「我标的那条期望答案到底死在哪一步」。R898 量 #02/#08 时就是靠另写脚本
复算才分清「被稀有词闸门删」和「被 cap 切」——每轮都重写一次脚本不叫可核查。

外部依据（见 output/迭代依据-R906-外部调研-大脑检索智能化-2026-11-06.md）：
menuagentic 的 fast diagnostic —— 拿答错的查询去语料里 grep 答案串，若答案在库里
却没进候选集，就是 retriever 的 bug，不是排序/prompt 的锅。本工具把这条做成
逐阶段、只读、可复跑的诊断，且**每一步都调 brain 的同一个函数、同一组参数**，
不写离屏打分器（记忆 #511：夹具不是生产形状等于没测）。

流水线（与 brain.prompt_recall 同序）：
  extract_prompt_terms → search_memories(limit=40, gate=False, semantic=False)
  → 曝光免疫 → hit >= need → best_raw_idf >= rare_line → pick_margin/cap
  → RECALL_SEM_RESCUE_*（弱候选可被语义救援捞回） → memory_ids

只读：不写库、不写 state、不触发 journal（prompt_recall 用 update_state=False /
origin=eval；语义路 rank 不落流水）。

用法：
    python tools/gap_diagnose_r906.py                    # 默认归因 #15/#24/#26
    python tools/gap_diagnose_r906.py --cases 2,13      # 指定样本编号（1-based）
    python tools/gap_diagnose_r906.py --all             # 全部 32 条
    python tools/gap_diagnose_r906.py --json            # 机器可读
"""
import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  控制台 UTF-8 守卫
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

DEFAULT_CASES = (15, 24, 26)
# 流水线阶段（按淘汰先后排序）。injected 是最终态，不是流水线阶段。
PIPELINE_STAGES = ("missing_in_db", "not_in_pool", "exposure_immune",
                   "hit_gate", "rare_gate", "passed_gates")
FINAL_INJECTED = "injected"


def _mem_meta(root: str, mid: int) -> dict:
    """只读回查：答案是不是还在库里、是不是 active、有没有被取代。

    评测集标注是人工写的，活库每天在变；不查这一步，一条已归档的答案会被
    误归因成「召回不行」，实际是标签过期（R891 同族教训）。
    """
    try:
        conn = sqlite3.connect(str(brain.db_path(root)))
        row = conn.execute(
            "SELECT status, superseded_by, substr(content, 1, 90) "
            "FROM memories WHERE id = ?", (int(mid),)).fetchone()
        conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return {"exists": False}
    if not row:
        return {"exists": False}
    try:
        superseded = int(row[1] or 0)
    except (TypeError, ValueError):
        superseded = 0
    return {"exists": True, "status": str(row[0] or ""),
            "superseded_by": superseded, "excerpt": str(row[2] or "")}


def _sem_map(root: str, prompt: str, limit: int = 40) -> dict:
    """与 prompt_recall 同一条语义路；未预热/异常一律空表（fail-open）。"""
    try:
        out = {}
        for eid, _rrf, row in brain._semantic_rank(root, prompt, "memories", limit=limit):
            sim = float(row.get("_semantic_sim") or 0.0)
            if sim > 0:
                out[int(row.get("id") or eid)] = sim
        return out
    except Exception:  # noqa: BLE001 可选能力，与生产同款兜底
        return {}


def diagnose_one(root: str, prompt: str, wants: list, *, session: str = "gap-diag",
                 state_path: str = "", recall_out: dict | None = None) -> dict:
    """对一条样本的**每一个期望答案**逐阶段归因。

    wants: [("mem", 507), ...]；记录路（"rec"）本轮不诊断，明确标 unsupported，
    不假装覆盖——三条 gap 的期望答案都在记忆路。
    """
    terms = brain.extract_prompt_terms(prompt)
    need = 2 if len(terms) >= 2 else 1
    query = " ".join(t for t, _ in terms)
    try:
        mems = brain.search_memories(root, query, limit=40, count_hits=False,
                                     semantic=False, gate=False)
    except (sqlite3.Error, OSError):
        mems = []
    by_id = {int(r.get("id") or 0): r for r in mems}
    texts = [str(r.get("content") or "") + " " + str(r.get("tags") or "")
             for r in mems]
    idf_raw = brain._recall_idf(texts, terms, weighted=False)
    rare_line = brain._recall_rare_threshold(len(mems))
    try:
        acct = brain._memory_counters(root, [r.get("id") for r in mems])
    except (sqlite3.Error, OSError):
        acct = {}
    sem = _sem_map(root, prompt)
    topn_ids = {i for i, _ in sorted(sem.items(), key=lambda kv: -kv[1])
                [:brain.RECALL_SEM_RESCUE_TOPN]}

    if recall_out is None:
        state = state_path or str(Path(tempfile.mkdtemp(prefix="gapdiag_")) / "state.json")
        recall_out = brain.prompt_recall(
            root, prompt, agent="workbuddy", session=session, state_path=state,
            update_state=False, origin="eval")
    final = [int(x) for x in (recall_out.get("memory_ids") or [])]
    # R861 语义桥：词法零候选时生产会走 `_semantic_rescue` 注入语义近邻，这些 id
    # 根本不在词法候选池里。不认这条路径就会把一次正常注入误标成「诊断与生产不一致」
    # （R906 首跑单测 2 红实锤），比不诊断还糟。
    bridge = int(recall_out.get("semantic_bridge") or 0) > 0

    answers = []
    for kind, mid in wants:
        mid = int(mid)
        rec = {"kind": kind, "id": mid, "pipeline_stage": "", "stage": "",
               "in_final": mid in final if kind == "mem" else False,
               "consistent": True}
        if kind != "mem":
            rec["pipeline_stage"] = "unsupported_record_path"
            rec["stage"] = "unsupported_record_path"
            answers.append(rec)
            continue
        meta = _mem_meta(root, mid)
        rec["exists"] = meta["exists"]
        rec["status"] = meta.get("status", "")
        rec["superseded_by"] = meta.get("superseded_by", 0)
        rec["excerpt"] = meta.get("excerpt", "")
        if not meta["exists"] or meta.get("status") != "active" or meta.get("superseded_by"):
            rec["pipeline_stage"] = "missing_in_db"
        elif mid not in by_id:
            rec["pipeline_stage"] = "not_in_pool"
        else:
            row = dict(by_id[mid])
            cnt = acct.get(mid) or {}
            if cnt:
                for k in ("inject_count", "adopt_count", "last_inject"):
                    row[k] = cnt[k]
            body = str(row.get("content") or "") + " " + str(row.get("tags") or "")
            low = body.lower()
            matched = [t for t, _ in terms if t in low]
            hit = brain._recall_hit_score(terms, body)
            best_raw = max((idf_raw.get(t, 0.0) for t in matched), default=0.0)
            sim = sem.get(mid, 0.0)
            eligible = (mid in topn_ids and sim >= brain.RECALL_SEM_RESCUE_SIM
                        and brain.RECALL_SEM_RESCUE_MODE != "off")
            rec.update(hit=hit, need=need, matched=matched,
                       best_raw=round(best_raw, 3), rare_line=round(rare_line, 3),
                       sem_sim=round(sim, 3), rescue_topn=mid in topn_ids,
                       rescue_eligible=eligible,
                       immune=bool(brain.is_exposure_immune(row)))
            if rec["immune"]:
                rec["pipeline_stage"] = "exposure_immune"
            elif hit < need:
                rec["pipeline_stage"] = "hit_gate"
            elif best_raw < rare_line:
                rec["pipeline_stage"] = "rare_gate"
            else:
                rec["pipeline_stage"] = "passed_gates"
        # 交叉校验放在**所有分支之后**：not_in_pool / missing_in_db 也可能被 R861
        # 语义桥注进 memory_ids，只在「答案在池里」那条分支里校验会漏掉（R906
        # 第二次单测实锤：consistent 保持初始 True、pipeline 停在 not_in_pool）。
        # 过了闸门却没进注入 = 被 margin/cap 切（正常）；没过闸门却进了注入，
        # 只有两条合法路径：R901 救援捞回 weak 候选、R861 语义桥注入池外近邻。
        ps, inf = rec["pipeline_stage"], rec["in_final"]
        if inf:
            rescued = ps == "rare_gate" and bool(rec.get("rescue_eligible"))
            bridged = bridge and ps in ("not_in_pool", "hit_gate")
            rec["consistent"] = (ps == "passed_gates" or rescued or bridged)
            if bridged:
                rec["pipeline_stage"] = "semantic_bridge"
            if not rec["consistent"]:
                rec["pipeline_stage"] = "inconsistent"
        rec["stage"] = FINAL_INJECTED if rec["in_final"] else rec["pipeline_stage"]
        answers.append(rec)

    return {"prompt": prompt, "terms": [t for t, _ in terms], "need": need,
            "pool_size": len(mems), "rare_line": round(rare_line, 3),
            "final_ids": final, "sem_scored": len(sem),
            "inconsistent": any(not a["consistent"] for a in answers),
            "answers": answers}


def _brain_root() -> str:
    import os
    env = os.environ.get("AGENTHUB_ROOT")
    if env:
        return env
    return str(ev._brain_root())


def render(report: dict, case_no: int) -> str:
    lines = [f"#{case_no:02d} {report['prompt']}",
             f"  terms={report['terms']} need={report['need']} "
             f"pool={report['pool_size']} rare_line={report['rare_line']} "
             f"sem_scored={report['sem_scored']} final_mem={report['final_ids']}"]
    for a in report["answers"]:
        if a["stage"] == "unsupported_record_path":
            lines.append(f"  - {a['kind']}#{a['id']} 记录路暂不诊断（明确标 unsupported）")
            continue
        detail = (f"hit={a.get('hit')}/{a.get('need')} "
                  f"best_raw={a.get('best_raw')} matched={a.get('matched')} "
                  f"sem_sim={a.get('sem_sim')} immune={a.get('immune')} "
                  f"rescue_eligible={a.get('rescue_eligible')}"
                  if "hit" in a else f"status={a.get('status')} "
                  f"superseded_by={a.get('superseded_by')}")
        lines.append(f"  - mem#{a['id']} stage={a['stage']} "
                     f"pipeline={a['pipeline_stage']} in_final={a['in_final']} | {detail}")
        if a.get("excerpt"):
            lines.append(f"      正文：{a['excerpt'][:78]}…")
        if not a["consistent"]:
            lines.append("      ⚠ 诊断与生产不一致：需人工复核（不许当结论用）")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="R906 零候选逐阶段归因（只读）")
    ap.add_argument("--cases", default=",".join(str(i) for i in DEFAULT_CASES))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--root", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    root = args.root or _brain_root()
    nums = list(range(1, len(ev.CASES) + 1)) if args.all else [
        int(x) for x in args.cases.split(",") if x.strip()]
    reports = []
    for n in nums:
        prompt, want, _why = ev.CASES[n - 1]
        rep = diagnose_one(root, prompt, ev._accepts(want))
        rep["case"] = n
        reports.append(rep)
        if not args.json:
            print(render(rep, n))
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=1))
    else:
        tally = {}
        for rep in reports:
            for a in rep["answers"]:
                tally[a["stage"]] = tally.get(a["stage"], 0) + 1
        print("\n阶段汇总：" + " · ".join(f"{k} {v}" for k, v in sorted(tally.items())))
        bad = sum(1 for r in reports if r["inconsistent"])
        print(f"诊断/生产一致性：不一致样本 {bad} 条（>0 时结论不可采信）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
