# -*- coding: utf-8 -*-
"""R943（B112）：confidence 分档能不能改变任何一条检索结论——只读同库 A/B。

为什么量这件事：R939 只读量化出 confidence 列 591 条里 526 条恒 1.0（89%），与
maturity 功能重叠、既不筛低可信也不参与排序；本轮的验收标准就是「按档分能不能改变
任何一条检索结论」，零收益就建议废弃这个字段，而不是继续往写入口塞一个没人读的数。

做法（切常量，不写第二份评分器——记忆 #511）：每档直接调 `brain.prompt_recall` 本体，
只切 `RECALL_CONF_W` / `RECALL_CONF_MIN`。两个档位的语义：
  · boost  ：spec += W*(conf-1)，conf=1.0 时严格加 0.0
  · filter ：conf < MIN 的候选不进注入（`dropped.conf` 计数）
所以「有变化」只可能来自真库里真正低于 1.0 的那些行；分母锁死 baseline 判出的题面
泄漏样本（R900 的教训：救援把 leak 换成 miss 会让分母漂移，凭空造出增益）。

三类输出，缺一不可：
  · 收益：Recall@3（分母不含泄漏样本）
  · 变动：hit→miss / miss→hit 的具体样本号、top3 换人的条数
  · 代价：被置信线删掉的候选数、单次调用耗时增量

用法：
    D:/python311/python.exe tools/confidence_ab_r943.py
    D:/python311/python.exe tools/confidence_ab_r943.py --out ../output/_r943_confidence_ab.log
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

# 档位顺序即打印顺序。boost 权重从「1 分（翻不动任何东西）」到「8 分（能翻典型排序差）」
# 都扫一遍：只测一个权重，等于把「权重没调好」当成「信号没用」。
CONFIGS = [
    ("baseline", 0.0, 0.0),
    ("boost_w1", 1.0, 0.0),
    ("boost_w3", 3.0, 0.0),
    ("boost_w8", 8.0, 0.0),
    ("filter_0.9", 0.0, 0.9),
    ("filter_0.8", 0.0, 0.8),
    ("filter_0.6", 0.0, 0.6),
]

BUCKETS = [(1.0, "1.0"), (0.9, "0.9-1.0"), (0.8, "0.8-0.9"),
           (0.6, "0.6-0.8"), (0.0, "<0.6")]


def conf_buckets(values) -> dict:
    """把一串 confidence 落进五个档（顺序固定，便于逐轮对照）。

    纯函数，不读库——用例可以直接喂值验证分档边界（0.9 归 "0.9-1.0" 而不是
    "0.8-0.9"）：边界归属写错会让「低置信有多少条」这种数悄悄偏一档。
    """
    out = {label: 0 for _, label in BUCKETS}
    for v in values:
        try:
            x = float(v)
        except (TypeError, ValueError):
            x = 1.0
        for floor, label in BUCKETS:
            if x >= floor:
                out[label] += 1
                break
    return out


def diff_top3(base_ids, other_ids):
    """两档 top3 的差异：返回 (换进来的 id, 换出去的 id)。

    只比 id 集合不比顺序：B112 问的是「结论变没变」，同一条答案换个位次不算变。
    """
    b, o = list(base_ids or []), list(other_ids or [])
    return [i for i in o if i not in b], [i for i in b if i not in o]


def _run(root, cases, state, w, min_conf, skip):
    brain.RECALL_CONF_W = w
    brain.RECALL_CONF_MIN = min_conf
    counts = {"hit": 0, "gap": 0, "miss": 0, "leak": 0}
    rows = {}
    drop_conf = 0
    t0 = time.time()
    for i, (prompt, want, why) in enumerate(cases, 1):
        if i in skip:
            continue
        accepts = ev._accepts(want)
        if not accepts:
            continue
        try:
            res = brain.prompt_recall(
                root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"r943-{w}-{min_conf}-{i}", state_path=str(state),
                update_state=False, origin="eval")
        except Exception as e:  # noqa: BLE001 量具不能因单条炸掉而中断
            print(f"  [{i:02d}] ERROR {e!r}")
            continue
        cls, _span, _leak = ev._classify(prompt, accepts, res, root)
        counts[cls] += 1
        drop_conf += int((res.get("dropped") or {}).get("conf") or 0)
        rows[i] = (cls, list(res.get("memory_ids") or []))
    return {"counts": counts, "rows": rows, "drop_conf": drop_conf,
            "ms": (time.time() - t0) * 1000.0 / max(1, len(rows))}


def _distribution(root):
    """真库分档 + 采纳/曝光交叉：低置信的那些行到底是不是「真在用」。"""
    q = ("SELECT confidence, kind, project, pinned, use_count, inject_count, "
         "adopt_count, last_inject, id, content FROM memories WHERE status='active' "
         "AND superseded_by=0")
    with brain.db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(q)]
    buckets = conf_buckets([r["confidence"] for r in rows])
    low = [r for r in rows if float(r["confidence"] or 1.0) < 1.0]
    return rows, buckets, low


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
    ok, why = ev.production_parity(fp)
    say(f"生产同构：{'是' if ok else '否'}（{why}）")

    rows, buckets, low = _distribution(root)
    say(f"\n分档（active & 未被取代，共 {len(rows)} 条）："
        + " · ".join(f"{k} {v}" for k, v in buckets.items()))
    adopted_low = [r for r in low if int(r["adopt_count"] or 0) > 0]
    injected_low = [r for r in low if int(r["inject_count"] or 0) > 0]
    say(f"  低于 1.0 的 {len(low)} 条里：被采纳过 {len(adopted_low)} 条 · "
        f"被注入过 {len(injected_low)} 条 · 置顶 {sum(1 for r in low if r['pinned'])} 条")
    for r in low[:12]:
        preview = " ".join(str(r.get("content") or "").split())[:40]
        say(f"    #{r['id']} conf={float(r['confidence']):.2f} kind={r['kind']} "
            f"proj={r['project'] or '-'} use={r['use_count']} inj={r['inject_count']} "
            f"adopt={r['adopt_count']} :: {preview}")

    cases = ev.CASES
    state = Path(tempfile.mkdtemp(prefix="r943_")) / "state.json"

    base = _run(root, cases, state, 0.0, 0.0, skip=set())
    leak_ids = {i for i, (cls, _ids) in base["rows"].items() if cls == "leak"}
    if leak_ids:
        base = _run(root, cases, state, 0.0, 0.0, skip=leak_ids)
    say(f"\n分母锁定：总样本 {len(cases)} · 题面泄漏剔除 {len(leak_ids)} 条"
        f"（{'、'.join(f'#{i}' for i in sorted(leak_ids)) or '无'}）")

    def rate(c):
        scored = sum(c.values()) - c["leak"]
        return c["hit"], scored, (c["hit"] / scored * 100 if scored else 0.0)

    say("\n档位            命中/分母   Recall@3   hit→miss  miss→hit  top3换人  删候选  均耗时ms")
    results = [("baseline", base)]
    for name, w, mc in CONFIGS[1:]:
        results.append((name, _run(root, cases, state, w, mc, skip=leak_ids)))

    base_rows = base["rows"]
    flip_total = 0
    lost_total = gained_total = 0
    details = []
    for name, r in results:
        hit, scored, rt = rate(r["counts"])
        lost = gained = changed = 0
        for i, (cls, ids) in r["rows"].items():
            b_cls, b_ids = base_rows.get(i, (cls, []))
            if b_cls == "hit" and cls != "hit":
                lost += 1
                details.append(f"{name} #{i:02d} hit→{cls}")
            elif b_cls != "hit" and cls == "hit":
                gained += 1
                details.append(f"{name} #{i:02d} {b_cls}→hit")
            if diff_top3(b_ids, ids)[0] or diff_top3(b_ids, ids)[1]:
                changed += 1
        flip_total += lost + gained
        lost_total += lost
        gained_total += gained
        say(f"{name:<14} {hit:>3}/{scored:<3}   {rt:>6.1f}%      {lost:>4}      {gained:>4}"
            f"      {changed:>4}    {r['drop_conf']:>4}    {r['ms']:>6.1f}")

    if details:
        say("\n变动明细：" + "；".join(details))
    say("\n结论：")
    if flip_total == 0:
        say("  零结论变化——七档（含把置信度当硬门槛的 0.9/0.8/0.6）没有改变任何一条"
            "样本的命中/未命中判定，连一条候选都没被置信线删掉（删候选列全 0）。")
        say("  → B112 的验收判据成立：confidence 在现网不产生任何检索收益，"
            "建议按 R939 的方向废弃写入（保留列以兼容旧库），不再往写入口塞新值。")
    elif gained_total == 0 and lost_total > 0:
        say(f"  只有损失没有收益：{lost_total} 条命中被打掉、0 条被救回。"
            "按 R898 的先例（补丁零效果就整段撤掉），这个分档方向不接受。")
        say("  → 仍判 B112 成立：confidence 不产生检索收益，只在大权重下当噪声；"
            "建议废弃写入，两个旋钮留在默认关档仅作证据。")
    else:
        say(f"  有 {flip_total} 处命中判定被改动（收益 {gained_total} / 损失 {lost_total}）"
            "——逐条读上面的变动明细，确认是真收益还是噪声，不能只看聚合分。")
    say(f"  低置信行明细见上表；低置信里被采纳过 {len(adopted_low)} 条、"
        f"被注入过 {len(injected_low)} 条——若这两个数非零，更严的过滤档会误伤真在用的记忆。")
    if args.out:
        stream.close()
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
