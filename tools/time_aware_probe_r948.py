# -*- coding: utf-8 -*-
"""R948（B116）：时间词在**记忆召回路径**上到底有没有生效——只读对照探针。

R944 逐文件核对的悬案：`brain._parse_time_range` 支持「最近/昨天/上周/本月/
十月初」（brain.py:7017），但只在 `:2483` 与 `:7492` 的**记录检索**里调用，
`prompt_recall` 记忆召回路径一次都没调。「别处有」不等于「这条路上有」，
也不等于「有危害」——先量出来再决定动不动手。

外部依据：LongMemEval（ICLR 2025）的 time-aware query expansion 对 temporal
推理任务带来 +7–11pp 召回；其前提是「提问里的时间范围被用来约束检索范围」。
见 output/迭代依据-R944-...md A1。

两条判据（写在前）：
  ① 忽略率：同一提问「含时间词」与「去掉时间词」两档注入的 id 集合完全相同，
     说明时间词对记忆召回没有任何影响（不是「影响小」，是一点都没有）。
  ② 越窗率：注入记忆的 created 不在 _parse_time_range 给出的窗口内的比例。
     库龄只有 38 天时，「十月初」这类窗口本就无对应记忆，此时硬塞别的时期
     的记忆就是可核查的越窗证据。

只读：不写库、不计唤起（update_state=False）、origin=probe（装置样本不进生产账）。
用法：
    D:/python311/python.exe tools/time_aware_probe_r948.py
    D:/python311/python.exe tools/time_aware_probe_r948.py --out ../output/_r948_time_aware.log
"""
import argparse
import datetime as dt
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  R940：tools/ 新脚本必须装控制台 UTF-8 守卫
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

# (含时间词的提问, 去掉时间词后的同义提问)。对照组只换时间词，不改实体与意图。
PAIRS = [
    ("上周的踩坑经验有哪些", "踩坑经验有哪些"),
    ("昨天改的召回逻辑怎么回滚", "改的召回逻辑怎么回滚"),
    ("这个月发布的版本有哪些", "发布的版本有哪些"),
    ("最近三天的门禁结果", "门禁结果"),
    ("十月初的部署记录", "部署记录"),
    ("上个月的发版证据在哪", "发版证据在哪"),
    ("今天的注入采纳率", "注入采纳率"),
    ("本月的记忆写入侧来源", "记忆写入侧来源"),
]


def window_hit(created: str, lo: str, hi: str) -> bool | None:
    """纯函数：入库日期是否落在 [lo, hi]（含端点）。格式坏返回 None，不猜。"""
    s = str(created or "")[:10]
    try:
        a, b, c = dt.date.fromisoformat(s), dt.date.fromisoformat(lo), dt.date.fromisoformat(hi)
    except (ValueError, TypeError):
        return None
    return b <= a <= c


def ignored_ratio(results: list) -> tuple:
    """纯函数：(两档注入 id 完全相同的条数, 可比较条数, 忽略率)。"""
    comparable = [r for r in results if r.get("window") and not r.get("error")]
    same = sum(1 for r in comparable
               if set(r.get("timed_ids") or []) == set(r.get("plain_ids") or []))
    return same, len(comparable), (same / len(comparable) if comparable else None)


def _created_map(root: str, ids: list) -> dict:
    if not ids:
        return {}
    ints = [int(i) for i in ids if str(i).lstrip("-").isdigit()]
    if not ints:
        return {}
    try:
        uri = brain.db_path(root).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except (sqlite3.Error, ValueError, OSError):
        return {}
    try:
        ph = ",".join("?" * len(ints))
        return {int(r[0]): str(r[1] or "")[:10] for r in
                conn.execute(f"SELECT id, created FROM memories WHERE id IN ({ph})", ints)}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _recall(root, prompt, state, i, tag):
    try:
        return brain.prompt_recall(
            root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
            session=f"r948-{tag}-{i}", state_path=str(state),
            update_state=False, origin="probe")
    except Exception as e:  # noqa: BLE001
        return {"memory_ids": [], "error": repr(e)}


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
    ok, why = ev.production_parity(fp)
    say(f"环境指纹 env={ev.env_key(fp)} · db={ev.db_key(fp)}"
        f"（memories {fp['memories']} / records {fp['records']}）")
    say(f"生产同构：{'是' if ok else '否'}（{why}）")
    say("只读对照：含时间词 vs 去时间词，同一份代码/同一个真库，只换提问里的时间表达。")

    state = Path(tempfile.mkdtemp(prefix="r948_")) / "state.json"
    results = []
    for i, (timed, plain) in enumerate(PAIRS, 1):
        try:
            window = brain._parse_time_range(timed)
        except Exception:  # noqa: BLE001
            window = None
        a = _recall(root, timed, state, i, "timed")
        b = _recall(root, plain, state, i, "plain")
        tids = list(a.get("memory_ids") or [])
        pids = list(b.get("memory_ids") or [])
        created = _created_map(root, tids)
        lo, hi = window if window else ("", "")
        hits = [window_hit(created.get(int(x), ""), lo, hi) for x in tids] if window else []
        inside = sum(1 for h in hits if h is True)
        outside = sum(1 for h in hits if h is False)
        results.append({"timed": timed, "plain": plain, "window": window,
                        "timed_ids": tids, "plain_ids": pids,
                        "inside": inside, "outside": outside,
                        "error": a.get("error") or b.get("error")})

    say("\n提问                              窗口                    注入  窗内  窗外  去词后相同")
    for r in results:
        lo, hi = r["window"] if r["window"] else ("-", "-")
        same = "是" if set(r["timed_ids"]) == set(r["plain_ids"]) else "否"
        say(f"{r['timed']:<32} {lo}~{hi:<12} {len(r['timed_ids']):>4}"
            f" {r['inside']:>5} {r['outside']:>5}  {same}"
            + (f"  ERROR {r['error']}" if r.get("error") else ""))

    same, comparable, ratio = ignored_ratio(results)
    total_inj = sum(len(r["timed_ids"]) for r in results)
    total_out = sum(r["outside"] for r in results)
    say(f"\n汇总：{len(PAIRS)} 组 · 可比较 {comparable} 组 · "
        f"两档注入完全相同 {same} 组（{(ratio*100 if ratio is not None else 0):.0f}%） · "
        f"含时间词档共注入 {total_inj} 条记忆、其中窗口外 {total_out} 条")

    say("\n结论：")
    if ratio is not None and ratio >= 0.75:
        say(f"  {ratio*100:.0f}% 的提问，去掉时间词后注入的记忆**逐条相同**——"
            "时间表达在记忆召回路径上被完全忽略（不是影响小，是一点都没有）。")
        say("  → B116 从「待确认」升级为「已量化的能力缺口」，但**本轮不改生产**："
            "R946 已证明动排序/闸门的代价容易外溢到 Recall@3；接时间限窗属功能增强，"
            "需要先设计「只约束、不重排」的最小实现并同库 A/B，登记为下一批候选。")
    else:
        say("  两档注入存在实质差异，时间词并非完全被忽略；逐条读上表再定性，"
            "不许凭聚合数下结论。")
    if total_inj and total_out:
        say(f"  另有 {total_out}/{total_inj} 条注入记忆的入库日期落在提问时间窗之外"
            "（越窗），是「没有按时间约束检索」的可核查证据。")
    say("  口径：装置样本 origin=probe 不进生产分母；窗口日期来自 brain._parse_time_range，"
        "与记录检索用的是同一个解析器。")
    if args.out:
        stream.close()
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
