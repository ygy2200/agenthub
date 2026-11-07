# -*- coding: utf-8 -*-
"""R968：hub_search 零命中只读取证 —— 2-gram 放宽到底兜住了没有。

日结（近 1 天）报「检索 1300 · 零命中 1（qoder: attributeerror|get|list）」。
大脑记忆 #437 说「中文长串零命中时自动 2-gram 放宽」已实现，但没人量过：
  · 全期零命中率到底多少、集中在哪个 agent/tool；
  · 零命中的 query 长什么样（中文长串 / 管道复合词 / 纯标识符 / 短词）；
  · 有没有「同词反复零命中」——这是索引或工具描述该改的硬信号（记忆 #437 口径）；
  · 中文长串仍零命中 = 2-gram 放宽没兜住，是真缺口的候选。

只读：SQLite mode=ro，不写库、不计唤起、不改配置。
判据（写在前）：
  · 若中文长串（≥8 个汉字）零命中 > 0 且 2-gram 子片段在库里确有内容 → 放宽失效，转轮次修；
  · 若零命中集中在「管道复合词 / 纯英文报错签名」且子片段也无命中 → 属输入形态问题，
    登记为引导/描述观察项，不改检索代码（#592 教训：别把业界坑当成自己的坑）。

用法：
    D:/python311/python.exe tools/search_zero_hit_probe_r968.py
    D:/python311/python.exe tools/search_zero_hit_probe_r968.py --out ../output/_r968_zero_hit.log
"""
import argparse
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  R940：tools/ 新脚本必须装控制台 UTF-8 守卫
import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")


def shape_of(q: str) -> str:
    """纯函数：零命中 query 的形态分类，互斥。"""
    s = (q or "").strip()
    if not s:
        return "empty"
    if "|" in s:
        return "pipe_compound"
    if len(CJK.findall(s)) >= 8:
        return "cjk_long"
    if CJK.search(s):
        return "cjk_short"
    if re.fullmatch(r"[A-Za-z0-9_.:/\\-]+", s):
        return "ascii_token"
    return "mixed"


def selftest():
    assert shape_of("中文长串连续八个汉字以上啊") == "cjk_long"
    assert shape_of("attributeerror|get|list") == "pipe_compound"
    assert shape_of("续跑点 状态卡") == "cjk_short"
    assert shape_of("hub_search") == "ascii_token"
    assert shape_of("") == "empty"
    print("SELFTEST OK | 5 组形态分类断言通过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    root = a.root or ev._brain_root()
    stream = open(a.out, "w", encoding="utf-8") if a.out else sys.stdout

    def say(s=""):
        print(s, file=stream)

    fp = ev.env_fingerprint(root)
    ok, why = ev.production_parity(fp)
    say(f"环境指纹 env={ev.env_key(fp)} · db={ev.db_key(fp)}"
        f"（memories {fp['memories']} / records {fp['records']}）")
    say(f"生产同构：{'是' if ok else '否'}（{why}）")
    conn = sqlite3.connect(brain.db_path(root).as_uri() + "?mode=ro",
                           uri=True, timeout=2.0)
    cutoff = brain._days_ago(a.days)
    rows = conn.execute(
        "SELECT ts, agent, tool, query, hits FROM searches WHERE ts >= ?",
        (cutoff,)).fetchall()
    all_n = conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
    say(f"\n窗口：近 {a.days} 天（全期 {all_n} 条，窗口内 {len(rows)} 条）")
    if not rows:
        say("分母为 0，方向当前不可验证（记忆 #566 口径）")
        conn.close()
        return 0
    zero = [r for r in rows if not r[4]]
    say(f"零命中 {len(zero)}/{len(rows)} = "
        f"{len(zero) / len(rows) * 100:.2f}%")

    say("\n按工具：")
    for tool, n in Counter(r[2] or "(blank)" for r in rows).most_common():
        z = sum(1 for r in zero if (r[2] or "(blank)") == tool)
        say(f"  {tool:<28} 检索 {n:>5} · 零命中 {z:>4} ({z / n * 100:.2f}%)")
    say("\n按 agent：")
    for ag, n in Counter(r[1] or "(blank)" for r in rows).most_common(8):
        z = sum(1 for r in zero if (r[1] or "(blank)") == ag)
        say(f"  {ag:<28} 检索 {n:>5} · 零命中 {z:>4}")

    shapes = Counter(shape_of(r[3]) for r in zero)
    say(f"\n零命中 query 形态：{dict(shapes)}")
    repeated = Counter((r[3] or "").strip() for r in zero)
    rep = [(q, n) for q, n in repeated.most_common(12) if q and n >= 2]
    say(f"同词反复零命中（≥2 次）：{len(rep)} 个")
    for q, n in rep:
        say(f"  {n:>3}× {q[:70]}")
    say("\n零命中样本（最近 15 条）：")
    for r in zero[-15:]:
        say(f"  {str(r[0])[:19]} {str(r[1]):<10} {shape_of(r[3]):<14} {str(r[3])[:64]}")

    cjk_long = [r for r in zero if shape_of(r[3]) == "cjk_long"]
    say(f"\n判据核对：中文长串（≥8 汉字）零命中 {len(cjk_long)} 条")
    for r in cjk_long[:8]:
        say(f"  {str(r[3])[:72]}")
    say("\n结论：")
    if cjk_long:
        say("  中文长串仍零命中 → 记忆 #437 的 2-gram 自动放宽**没有兜住这一批**，"
            "转下一轮逐条核对子片段在库里是否真有内容，再决定动不动检索代码。")
    elif rep:
        say("  无中文长串零命中（2-gram 放宽生效）；但存在同词反复零命中 → "
            "按记忆 #437 口径优先查工具描述/索引，别先改检索算法。")
    else:
        say("  既无中文长串零命中、也无同词反复零命中 → 零命中属零散输入形态，"
            "2-gram 放宽已覆盖，**不动代码**，登记为观察项（#592 教训）。")
    say("  口径：hits=0 记为零命中；只读 mode=ro；装置样本与生产样本未拆分，"
        "本轮只看形态不下优先级结论。")
    conn.close()
    if a.out:
        stream.close()
        print(f"已写入 {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
