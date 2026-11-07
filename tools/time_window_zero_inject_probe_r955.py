# -*- coding: utf-8 -*-
"""R955：开窗后「零注入」归因 —— 窗内没候选，还是闸门把窗内候选删光了。

R954 A/B 三条判据全过（越窗 -100%、Recall@3 +0、噪声 +0），但 8 组时间词
提问里 5 组开窗后一条都不注入，注入总 8→4。这个代价不在预设判据里，
不许凭「指标全绿」就改默认档。

本探针只读，回答三个逐层问题：
  ① 全库在这个时间窗里到底有多少条记忆/记录？（库里有，才有得谈）
  ② 检索取回的 top-N 候选里，窗内/窗外/无日期各几条？（检索池覆盖问题）
  ③ 窗内候选 > 0 却零注入的组，`dropped` 四类计数落在哪一层？（闸门叠加问题）

口径全部复用生产同款：extract_prompt_terms 拼 query、_memory_dates（updated
优先/created 兜底）判记忆日期、_row_date 判记录日期、RECALL_POOL_LIMIT=40。
只读：update_state=False、origin=probe、只读 SQLite 连接，不写库、不计唤起。

用法：
    D:/python311/python.exe tools/time_window_zero_inject_probe_r955.py
    D:/python311/python.exe tools/time_window_zero_inject_probe_r955.py --out ../output/_r955_zero_inject.log
"""
import argparse
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
import time_aware_probe_r948 as r948  # noqa: E402


def _ro_conn(root):
    return sqlite3.connect(brain.db_path(root).as_uri() + "?mode=ro",
                           uri=True, timeout=2.0)


def _whole_db(root, lo, hi):
    """全库该窗口内的记忆/记录条数（不局限于检索 top-N）。"""
    try:
        conn = _ro_conn(root)
    except (sqlite3.Error, ValueError, OSError):
        return None, None
    try:
        m = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE "
            "substr(COALESCE(NULLIF(updated,''),created),1,10) BETWEEN ? AND ?",
            (lo, hi)).fetchone()[0]
        r = conn.execute(
            "SELECT COUNT(*) FROM records WHERE substr(date,1,10) BETWEEN ? AND ?",
            (lo, hi)).fetchone()[0]
        return int(m), int(r)
    except sqlite3.Error:
        return None, None
    finally:
        conn.close()


def _split(rows, datefn, lo, hi):
    inside = outside = unknown = 0
    for r in rows:
        d = datefn(r)
        if not d:
            unknown += 1
        elif lo <= d <= hi:
            inside += 1
        else:
            outside += 1
    return inside, outside, unknown


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
    say("只读归因：全库窗内条数 → 检索 top-N 窗内条数 → 注入条数 → 被哪层删掉。")
    brain.RECALL_TIME_WINDOW = 1
    state = Path(tempfile.mkdtemp(prefix="r955_")) / "state.json"

    say("\n提问                            窗口            全库(记/录)  候选窗内(记/录)"
        "  窗内外/无日期(记/录)      注入  dropped")
    zero_with_candidates = 0
    zero_without_candidates = 0
    for i, (timed, _plain) in enumerate(r948.PAIRS, 1):
        window = brain._parse_time_range(timed)
        if not window:
            say(f"{timed:<30} 无窗口（跳过）")
            continue
        lo, hi = window
        terms = brain.extract_prompt_terms(timed)
        query = " ".join(t for t, _ in terms)
        try:
            mems = brain.search_memories(root, query, limit=brain.RECALL_POOL_LIMIT,
                                         count_hits=False, semantic=False, gate=False)
            recs = brain.search_records(root, query, limit=30)
        except sqlite3.Error as e:
            say(f"{timed:<30} 检索失败 {e!r}")
            continue
        mdates = brain._memory_dates(root, [r.get("id") for r in mems])
        mi, mo, mu = _split(mems,
                            lambda r: mdates.get(int(r.get("id") or 0), "") or brain._row_date(r),
                            lo, hi)
        ri, ro, ru = _split(recs, brain._row_date, lo, hi)
        whole_m, whole_r = _whole_db(root, lo, hi)
        try:
            res = brain.prompt_recall(
                root, timed, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"r955-{i}", state_path=str(state),
                update_state=False, origin="probe")
        except Exception as e:  # noqa: BLE001
            say(f"{timed:<30} 召回失败 {e!r}")
            continue
        inj = len(res.get("memory_ids") or []) + len(res.get("record_ids") or [])
        d = res.get("dropped") or {}
        dsum = "/".join(str(d.get(k, 0)) for k in ("hit", "rare", "immune", "cap"))
        say(f"{timed:<30} {lo}~{hi}  "
            f"{whole_m:>4}/{whole_r:<4}  {mi:>6}/{ri:<6}  "
            f"{mi}/{mo}/{mu} · {ri}/{ro}/{ru:<12}  {inj:>3}  {dsum}"
            f"  [tw剔除 {res.get('time_window_dropped') or 0}]")
        if inj == 0:
            if mi + ri > 0:
                zero_with_candidates += 1
            else:
                zero_without_candidates += 1

    say(f"\n零注入分组：窗内确有候选但一条没进 {zero_with_candidates} 组 · "
        f"窗内本就没候选 {zero_without_candidates} 组")
    say("\n结论：")
    if zero_with_candidates:
        say(f"  {zero_with_candidates} 组「窗内有候选却零注入」→ 限窗与既有闸门"
            "（hit/rare/cap）叠加过严，是真缺口，不能靠改默认档绕过；"
            "需要单独设计「窗内候选的补位/降门槛」，且必须再跑同库 A/B。")
    if zero_without_candidates:
        say(f"  {zero_without_candidates} 组「窗内本就没候选」→ 检索按相关性取 top-N，"
            "不保证覆盖时间窗；属召回池深度问题，不是日期口径错误。")
    say("  口径：全库窗内数按 substr(date,1,10) 计；候选窗内数按生产同款口径；"
        "dropped 四列依次 hit/rare/immune/cap；装置样本 origin=probe。")
    if args.out:
        stream.close()
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
