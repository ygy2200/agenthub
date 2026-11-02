# -*- coding: utf-8 -*-
"""R834 复盘探针：error_recall 零命中 55.6% 到底是哪一层挡的。

为什么要它：R833 收尾线上数据显示 error_recall 空转率 55.6%（n=36，20 条零命中），
而 prompt_recall 只有 1%。光看这个数字没法决定改什么——「库里确实没记过」和
「记过但检索没捞上来」是两种完全不同的修法。本脚本按层归因，只读、不记账。

口径（照 memory #511：喂线上 payload 形状，不喂干净文本）：
  用例 = R833 冒烟集那 9 条真报错 + 本轮线上新抓到的形状，直接走 brain.error_recall(count=False)
  真实入口拿签名，再用同一批 terms/权重离线复刻精度闸门，保证判据与生产一致。

归因分桶：
  E_hit      真注入了（基线，无需修）
  D_skip     没进检索（no_signature / self_tool / seen_this_session）
  A_absent   全库暴力扫都找不到一条含招牌词的（真·知识缺失，检索放宽救不了）
  B_gate     词法本有能过精度闸门的候选，被覆盖度闸门清零（可回收的假阴性）
  C_truncate 全库有能过闸门的记忆/记录，但通道 limit 排序没捞进来（召回截断）
  C_precision 候选捞进来了但精度闸门判不够招牌（精度正确，本该不注入）
用法：python tools/error_recall_coverage_probe_r834.py [--root D:\\agent资料\\zcode项目记录]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _console  # noqa: E402,F401  让 Windows 控制台能吃中文
import brain  # noqa: E402

# 前 9 条 = R833 冒烟集原样（已被证明是线上形状），后 3 条 = 本轮线上真撞到的形状
CASES = [
    ("Bash", "sqlite3.OperationalError: no such column: inject_count"),
    ("Bash", "ModuleNotFoundError: No module named 'marker_pdf'"),
    ("mcp__agenthub__hub_graph", "ValueError: invalid literal for int() with base 10: 'abc'"),
    ("Bash", "PermissionError: [WinError 5] 拒绝访问。: 'C:/Program Files/x.exe'"),
    ("Bash", "Error: connect ECONNREFUSED 127.0.0.1:7890"),
    ("Bash", "Traceback (most recent call last):\n"
             '  File "D:/a/b.py", line 12, in main\n'
             "    x = int(v)\n"
             "TypeError: int() argument must be a string, a bytes-like object "
             "or a number, not NoneType"),
    ("Write", "命令失败了"),
    ("Bash", "git: command not found"),
    ("py_compile", "SyntaxError: (unicode error) 'unicodeescape' codec can't decode "
                   "bytes in position 100-101: truncated \\xXX escape"),
    # 2026-11-02 线上真投递过的三条（journal/searches 抓回）
    ("Bash", "UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713' in "
             "position 42: illegal multibyte sequence"),
    ("Bash", 'File "D:/agenthub/cli.py", line 88, in cmd_deploy\n'
             "can't open file '~/.agenthub/hooks/brain_error_recall.py': "
             "[Errno 2] No such file or directory"),
    ("node", "Error: ENOENT: no such file or directory, open "
             "'D:/agent资料/hooks/brain_error_recall.mjs'"),
]


def _accept(terms: list) -> callable:
    """与 brain.error_recall 内的 `_accept` 同判据（复刻用于离线归因，不改生产）。"""
    high = [t for t, w in terms if w >= 3]

    def _acc(low: str) -> bool:
        if not high or not any(h in low for h in high):
            return False
        if any(brain._err_distinctive(h) and h in low for h in high):
            return True
        return sum(1 for t, w in terms if w >= 2
                   and brain._err_distinctive(t) and t in low) >= 2
    return _acc


def _truth_scan(root: str, acc: callable) -> dict:
    """全库暴力扫：到底有没有「能过精度闸门」的记忆/记录，与检索通道无关。"""
    with brain.db_conn(root) as conn:
        mems = [dict(r) for r in conn.execute(
            "SELECT id, content, tags, kind FROM memories").fetchall()]
        recs = [dict(r) for r in conn.execute(
            "SELECT id, title, content, project FROM records").fetchall()]
    m_hit = [r["id"] for r in mems
             if acc((str(r.get("content") or "") + " "
                     + str(r.get("tags") or "")).lower())]
    r_hit = [r["id"] for r in recs
             if acc((str(r.get("title") or "") + " "
                     + str(r.get("content") or "")).lower())]
    return {"mem_truth": m_hit, "rec_truth": r_hit}


def _probe(root: str, tool: str, text: str) -> dict:
    terms = brain.error_signature_terms(brain._error_voice(text))
    acc = _accept(terms)
    res = brain.error_recall(root, text, tool=tool, agent="probe",
                             session="", update_state=False, count=False)
    out = {"terms": terms, "res": res, "skipped": res["skipped"]}
    if res["text"]:
        out["class"] = "E_hit"
        return out
    if res["skipped"] and res["skipped"] != "no_match":
        # no_signature / self_tool / seen_this_session：根本没进检索，归因不关检索的事
        out["class"] = "D_skip"
        return out
    truth = _truth_scan(root, acc)
    query = " ".join(t for t, w in terms if w >= 2)[:180] or " ".join(t for t, _ in terms)
    words = [w for w in re.split(r"\s+", query) if w][:8]
    pre = brain._records_query(root, words, 100)
    post = brain.search_records(root, query, limit=20)
    mems = brain.search_memories(root, query, limit=25, count_hits=False,
                                 semantic=False, gate=False)
    toks = list(dict.fromkeys(brain._tokenize(query, max_tokens=16)))
    df = brain._token_df(root, toks)
    out.update(
        query=query,
        df0=[t for t in toks if df.get(t, 1) == 0 and t.isascii()],
        pre_acc=[r["id"] for r in pre
                 if acc((str(r.get("title") or "") + " "
                         + str(r.get("content") or "")).lower())],
        post_acc=[r["id"] for r in post
                  if acc((str(r.get("title") or "") + " "
                          + str(r.get("content") or "")).lower())],
        mem_acc=[r["id"] for r in mems
                 if acc((str(r.get("content") or "") + " "
                         + str(r.get("tags") or "")).lower())],
        **truth)
    if not out["mem_truth"] and not out["rec_truth"]:
        out["class"] = "A_absent"
    elif out["pre_acc"] and not out["post_acc"]:
        out["class"] = "B_gate"
    elif (out["mem_truth"] and not out["mem_acc"]) or (out["rec_truth"] and not out["post_acc"]):
        out["class"] = "C_trunc"
    else:
        out["class"] = "C_precision"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    args = ap.parse_args()
    import core
    root = args.root or core.get_root()
    if not root or not Path(root).is_dir():
        print(f"× 大脑根不可用：{root!r}")
        return 2
    tally: dict = {}
    for tool, text in CASES:
        p = _probe(root, tool, text)
        tally[p["class"]] = tally.get(p["class"], 0) + 1
        print("=" * 78)
        print(f"[{p['class']}] {tool} · {text[:64].replace(chr(10), ' ')}")
        print(f"  签名 {[(t, w) for t, w in p['terms'][:6]]}")
        if p["class"] in ("E_hit", "D_skip"):
            print(f"  skipped={p['skipped'] or '-'} "
                  f"mem={p['res']['memory_ids']} rec={p['res']['record_ids']} "
                  f"err={p['res']['error_ids']}")
            continue
        print(f"  query={p['query'][:90]!r}")
        print(f"  df0（库里不存在的拉丁词元）={p['df0']}")
        print(f"  全库能过精度闸门：记忆 {p['mem_truth'][:6]} / 记录 {p['rec_truth'][:6]}")
        print(f"  检索通道捞到并过闸门：记忆 {p['mem_acc'][:6]} / 记录 {p['post_acc'][:6]}"
              f"（闸门前 {len(p['pre_acc'])} 条）")
    n = len(CASES)
    hit = tally.get("E_hit", 0)
    print("=" * 78)
    print(f"共 {n} 条 · 注入 {hit}（{hit / n:.0%}）· 归因 " +
          " · ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
