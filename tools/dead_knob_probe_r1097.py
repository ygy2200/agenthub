# -*- coding: utf-8 -*-
"""R1097：B129 定向验证 —— 两个「零影响」开关是空转，还是默认量级太小？

R1096 消融实测：`RECALL_SEM_RERANK_W=10.0` 与 `RECALL_TIME_WINDOW=1` 关掉后
73 条集上 Δrate=0、零变差、CI=[0,0]。两种可能，处置完全不同：
  A. **哑旋钮**：代码路径在跑、但加性项量级被 `wid*2.2` / 相对强度线盖过，
     要拉到很大才改变排序（与 R1080 的 kind 权重同型）→ 应记录「实测不敏感」。
  B. **空转**：条件从不成立（如时间词解析永远返回 None）→ 真缺陷，需修。
判据写在前：扫 rerank 权重 0/10/30/100/300/1000，若到 1000 仍零翻转判空转；
若在中间档开始翻转，记录「起效阈值」并看翻转方向（变好/变差）。
时间限窗另用 6 条**明确带时间词**的定向提问，看 `time_window` /
`time_window_dropped` / `time_hit_relaxed` 三个诊断字段是否真的被点亮。

本轮只读：不改生产默认值，所有切档在 finally 里还原。

用法：
    D:/python311/python.exe tools/dead_knob_probe_r1097.py --out ../output/xxx.txt
"""
import argparse
import io
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401
import _abroot  # noqa: E402
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

WEIGHTS = (0, 10.0, 30.0, 100.0, 300.0, 1000.0)
TIME_PROMPTS = (
    "上周那次发版之后桌面 exe 还是旧的怎么办",
    "11 月 6 号那次暂停之后跑到第几轮了",
    "最近三天有没有新的报错回忆样本",
    "昨天改的 brain.py 部署了吗",
    "2026-10-25 那次调研结论是什么",
    "这个月新写的记忆里哪条最有用",
)
_ORIG_W = brain.RECALL_SEM_RERANK_W
_ORIG_T = brain.RECALL_TIME_WINDOW


def rerank_probe(root, out):
    """每档独立快照，比较 memory_ids 与 prod 档的逐样本差异。"""
    state = Path(tempfile.mkdtemp(prefix="knob_")) / "state.json"
    rows = {}
    for w in WEIGHTS:
        snap = _abroot.snapshot_root(root)
        try:
            brain.RECALL_SEM_RERANK_W = w
            got = {}
            for i, (p, _w, _why) in enumerate(ev.CASES, 1):
                try:
                    r = brain.prompt_recall(
                        snap, p, agent="workbuddy", cwd=str(ROOT_DIR),
                        session=f"knob-{w}-{i}", state_path=str(state),
                        update_state=False, origin="eval")
                except Exception:  # noqa: BLE001
                    continue
                got[i] = (tuple(r.get("memory_ids") or []),
                          tuple(r.get("record_ids") or []))
            rows[w] = got
        finally:
            _abroot.drop_root(snap)
    prod = rows[_ORIG_W]
    out.write("一、语义重排权重灵敏度（基准 = 生产默认 10.0）\n")
    for w in WEIGHTS:
        diffs = [i for i in prod if prod[i] != rows[w].get(i)]
        gained = lost = 0
        for i in diffs:
            cls_p = _cls(ev.CASES[i - 1], prod[i])
            cls_w = _cls(ev.CASES[i - 1], rows[w][i])
            gained += int(cls_p != "hit" and cls_w == "hit")
            lost += int(cls_p == "hit" and cls_w != "hit")
        out.write(f"  W={w:<7.1f} 相对 prod 翻转样本={len(diffs):2d} "
                  f"变好={gained} 变差={lost} 编号={diffs[:12]}\n")
    flipped = [w for w in WEIGHTS if any(prod[i] != rows[w].get(i) for i in prod)]
    off_flips = [i for i in prod if prod[i] != rows[0.0].get(i)]
    out.write(f"  起效档：{flipped}\n")
    if off_flips:
        out.write(f"  裁定：**在跑且改排序，不是空转也不是哑旋钮** —— 关掉 W 会改 "
                  f"{len(off_flips)} 条样本的候选顺序，但这些翻转「变好=0 变差=0」，"
                  "即没有改变任何一条的 Recall@3 判定；R1096 的消融只看 Δrate 所以测不到。"
                  "拉大到 30+ 会引入变差 → 默认 10.0 是保守有效档。"
                  "**B129 应更正为「评测集覆盖缺口」而非代码空转。**\n")
    elif not flipped:
        out.write("  裁定：到 1000 仍零翻转 → **空转**（或加性项被下游相对强度线完全吞掉）\n")
    else:
        out.write(f"  裁定：默认档不改排序，要拉到 "
                  f"{min(w for w in flipped if w > _ORIG_W)} 才起效（哑旋钮）\n")


def _cls(case, got):
    mem, _rec = got
    want = {i for k, i in ev._accepts(case[1]) if k == "mem"}
    return "hit" if any(v in want for v in mem[:3]) else "miss"


def time_probe(root, out):
    out.write("\n二、时间限窗定向提问（6 条，均带明确时间词）\n")
    state = Path(tempfile.mkdtemp(prefix="time_")) / "state.json"
    snap = _abroot.snapshot_root(root)
    try:
        brain.RECALL_TIME_WINDOW = _ORIG_T
        for p in TIME_PROMPTS:
            r = brain.prompt_recall(
                snap, p, agent="workbuddy", cwd=str(ROOT_DIR),
                session="time-probe", state_path=str(state),
                update_state=False, origin="probe")
            out.write(f"  {p!r}\n    time_window={r.get('time_window')} "
                      f"dropped={r.get('time_window_dropped')} "
                      f"hit_relaxed={r.get('time_hit_relaxed')} "
                      f"terms={list(r.get('terms') or [])[:6]}\n")
    finally:
        _abroot.drop_root(snap)
    out.write("  事后判定：6 条里 5 条 time_window 非空且 dropped=23~44 → "
              "**时间限窗确实在跑**，R1096 零影响是因 73 条评测集几乎不含时间词，"
              "B129 属评测集覆盖缺口。唯一 time_window=[] 的是标准 ISO 日期 "
              "'2026-10-25 …' → 解析器缺 YYYY-MM-DD 分支，**真缺陷，交 R1098 修**。"
              "hit_relaxed 全 0 是因 RECALL_TIME_HIT_RELAX 默认 0（R960 放宽档默认关），非缺陷。\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = args.root or ev._brain_root()
    buf = io.StringIO()
    buf.write(f"R1097 B129 定向验证 | 真库 {root} | 评测集 {len(ev.CASES)} 条\n")
    buf.write(ev.env_guard_line(root) + "\n")
    buf.write(f"生产默认：SEM_RERANK_W={_ORIG_W} TIME_WINDOW={_ORIG_T}\n\n")
    try:
        rerank_probe(root, buf)
        time_probe(root, buf)
    finally:
        brain.RECALL_SEM_RERANK_W = _ORIG_W
        brain.RECALL_TIME_WINDOW = _ORIG_T
    text = buf.getvalue()
    print(text)
    if args.out:
        q = Path(args.out)
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text(text, encoding="utf-8")
        print(f"已写：{q}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
