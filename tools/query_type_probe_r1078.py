# -*- coding: utf-8 -*-
"""R1078：query-type 判别的第一步是**量分布**，不是先改代码。

续跑点 R1078 的假设：问「为什么 / 怎么查 / 哪一版」对候选的要求不同，低分带
收益始终 +1/+1 对冲说明缺的是问法信号而不是阈值。但 brain._recall_intent 自
R820 起就存在，只用来调 RECALL_KIND_WEIGHTS；从来没人量过它在 53 条评测集上
判成了什么、各档命中率差多少、gap 集中在哪一档。没这张表就动手改阈值，等于
把 R1077 的「+1 命中 / +1 噪声」再演一遍。

外部依据（2026-11-10 检索）：
  · Query type awareness：informational 问句可过滤弱意图词，navigational/exact
    问句必须保留；疑问模板应当**单独记意图**，不必然从检索式删掉
    https://frutik.github.io/awesome-search/Concepts/Stopwords
  · 中文 RAG query 清洗：维护「停用词 + 弱意图词表」，疑问模板可另记意图
    https://m.hr-soft.cn/blogkeji/2026062447198.html
  · 停用词不能无脑删：短查询每个词都重要；意图分类/QA 场景功能词本身就是
    问法信号（"Who approved" vs "What approved" 是不同问题类型）
    https://datarekha.com/interview/nlp-llm/stop-words-in-nlp
  · 判据用 recall@k 在未触碰评测集上验证，且训练/服务两侧预处理必须一致
    同上；另见 https://inferensys.com/glossary/semantic-search-and-entity-recognition/query-expansion-techniques/stop-word-removal

本工具**只读**：不改任何常量、不写库（真库快照隔离，避免注入计数污染下一轮）。

用法：
    D:/python311/python.exe tools/query_type_probe_r1078.py
    D:/python311/python.exe tools/query_type_probe_r1078.py --out ../output/xxx.txt
"""
import argparse
import io
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

# 只用于观测「这句是不是疑问句」，不参与任何删除动作。
QUESTION_MARKS = (
    "为什么", "为何", "怎么", "如何", "是不是", "到底", "还是", "哪一", "哪块",
    "哪", "吗", "呢", "会不会", "能不能", "行不行", "什么时候", "多少",
    "算不算", "真的吗", "先查", "是什么原因", "哪一步",
)


def _qmarks(prompt: str) -> list:
    return [w for w in QUESTION_MARKS if w in prompt]


def run(root: str, out: str = "") -> int:
    cases = ev.CASES
    state = Path(tempfile.mkdtemp(prefix="qtype_probe_")) / "state.json"
    buf = io.StringIO()
    buf.write(f"R1078 query-type 分布只读诊断 | 真库 {root} | 样本 {len(cases)} 条\n")
    buf.write(ev.env_guard_line(root) + "\n\n")

    rows = []
    snap = _abroot.snapshot_root(root)
    try:
        for i, (prompt, want, why) in enumerate(cases, 1):
            accepts = ev._accepts(want)
            if not accepts:
                continue
            intent = brain._recall_intent(prompt)
            terms = [t for t, _ in brain.extract_prompt_terms(prompt)]
            try:
                res = brain.prompt_recall(
                    snap, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                    session=f"qtype-{i}", state_path=str(state),
                    update_state=False, origin="eval")
            except Exception as exc:  # noqa: BLE001 单条异常不中断量具
                buf.write(f"[{i:02d}] ERROR {prompt!r}: {exc}\n")
                continue
            cls, _span, _leak = ev._classify(prompt, accepts, res, snap)
            dropped = dict(res.get("dropped") or {})
            rows.append({"i": i, "prompt": prompt, "why": why, "intent": intent,
                         "terms": terms, "qmarks": _qmarks(prompt), "cls": cls,
                         "dropped": dropped,
                         "sem_active": int(bool(res.get("sem_scored"))),
                         "n_mem": len(res.get("memory_ids") or [])})
    finally:
        _abroot.drop_root(snap)

    buckets = defaultdict(list)
    for r in rows:
        buckets[r["intent"]].append(r)

    buf.write("一、按 _recall_intent 分桶（分母含 gap，不含 leak）\n")
    order = ("howto", "pref", "status", "general")
    for name in order:
        rs = buckets.get(name, [])
        if not rs:
            buf.write(f"  {name:<8} 0 条\n")
            continue
        cnt = defaultdict(int)
        for r in rs:
            cnt[r["cls"]] += 1
        scored = len(rs) - cnt["leak"]
        hit = cnt["hit"]
        buf.write(f"  {name:<8} {len(rs):2d} 条  hit={hit:2d} gap={cnt['gap']:2d} "
                  f"miss={cnt['miss']:2d} leak={cnt['leak']:2d}  "
                  f"Recall@3={hit}/{scored}={hit / scored * 100 if scored else 0:.1f}%\n")

    buf.write("\n二、疑问句标记 vs 非疑问句（口径独立于 intent，用来量 intent 判得准不准）\n")
    for label, sel in (("带疑问词", [r for r in rows if r["qmarks"]]),
                       ("不带疑问词", [r for r in rows if not r["qmarks"]])):
        cnt = defaultdict(int)
        for r in sel:
            cnt[r["cls"]] += 1
        scored = len(sel) - cnt["leak"]
        hit = cnt["hit"]
        rare = sum(int(r["dropped"].get("rare") or 0) for r in sel)
        buf.write(f"  {label:<10} {len(sel):2d} 条  hit={hit:2d} gap={cnt['gap']:2d} "
                  f"miss={cnt['miss']:2d}  Recall@3={hit / scored * 100 if scored else 0:.1f}%"
                  f"  rare删除合计={rare}\n")

    buf.write("\n三、intent 判错嫌疑：带疑问词却判成 general / status，或反之\n")
    suspects = [r for r in rows
                if (r["qmarks"] and r["intent"] in ("general", "status", "pref"))
                or (not r["qmarks"] and r["intent"] == "howto")]
    for r in suspects:
        buf.write(f"  [{r['i']:02d}] intent={r['intent']:<8} 疑问词={r['qmarks']} "
                  f"cls={r['cls']:<4} {r['prompt']!r}\n")
    buf.write(f"  合计 {len(suspects)}/{len(rows)} 条判错嫌疑\n")

    buf.write("\n四、gap / miss 样本逐条（问法信号是否集中在失败样本）\n")
    for r in rows:
        if r["cls"] in ("gap", "miss"):
            buf.write(f"  [{r['i']:02d}] {r['cls']:<4} intent={r['intent']:<8} "
                      f"疑问词={','.join(r['qmarks']) or '无'}\n"
                      f"        {r['prompt']!r}\n"
                      f"        terms={r['terms']} dropped={r['dropped']}\n")

    buf.write("\n五、结论口径（写在前，防事后找理由）\n")
    buf.write("  · 只要能证明「失败样本集中在某一 intent 档」，才值得做分档闸门 A/B；\n")
    buf.write("  · 若意图判别本身大面积误判（嫌疑 > 20%），先修判别，别修阈值；\n")
    buf.write("  · 任何后续改动的判据沿用 R1074：Δrate>0 且 Δ噪声<=0 且 Δ耗时<=50ms。\n")
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
    return run(args.root or ev._brain_root(), args.out)


if __name__ == "__main__":
    raise SystemExit(main())
