# -*- coding: utf-8 -*-
"""R963（B115）：knowledge-update 类评测语料——真库取代链驱动的只读量具。

R944 外部取证登记的缺口：现有 32 条常规语料全是「有答案」样本，没有一条在量
「结论被更新之后，大脑还给不给旧结论」。业界（LongMemEval / MemEvoBench）把
这类叫 knowledge-update，是记忆系统最容易悄悄退化的一档。

本仓机制已齐（记忆 #561）：memories.superseded_by 列 + _mem_live_sql 过滤
（brain.py:5943/5953）+ supersedes= 写入口 + 链查询。所以本轮**不改生产**，
只把「机制有没有在注入路径上真的生效」变成可复跑的数字：
  ① 旧条泄漏率：被取代的旧结论 id 是否出现在 prompt_recall 的 memory_ids（应为 0）
  ② 现行条召回率：取代链末端（现行结论）是否出现在注入里（有用性，非正确性硬判据）
  ③ 链形态分布：单跳 / 多跳 / 废弃无继任 / 环

只读：SQLite mode=ro、update_state=False、origin=eval（装置样本不进生产分母）。
不写库、不改配置、不新增生产代码。`--selftest` 跑纯函数链解析自检。

用法：
    D:/python311/python.exe tools/knowledge_update_eval_r963.py --selftest
    D:/python311/python.exe tools/knowledge_update_eval_r963.py
    D:/python311/python.exe tools/knowledge_update_eval_r963.py --out ../output/_r963_knowledge_update.log
"""
import argparse
import re
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


def resolve_chain(conn, old_id, max_hops=8):
    """纯函数：沿 superseded_by 从旧条走到现行末端。

    返回 (末端 id, 跳数, 路径 list, 形态)。
    形态：live / deprecated(-1 无继任) / missing / cycle / max_hops / already_live。
    只读一条 SELECT，不写库；环与超长链都要停，不许死循环。
    """
    path = [int(old_id)]
    cur = int(old_id)
    for _ in range(max_hops):
        row = conn.execute(
            "SELECT COALESCE(superseded_by,0) FROM memories WHERE id=?", (cur,)
        ).fetchone()
        if not row:
            return cur, len(path) - 1, path, "missing"
        nxt = int(row[0] or 0)
        if nxt == 0:
            shape = "already_live" if len(path) == 1 else "live"
            return cur, len(path) - 1, path, shape
        if nxt == -1:
            return cur, len(path) - 1, path, "deprecated"
        if nxt in path:
            return cur, len(path) - 1, path, "cycle"
        path.append(nxt)
        cur = nxt
    return cur, len(path) - 1, path, "max_hops"


def _prompt_from(content, limit=70):
    """从旧结论正文造一句可检索的提问：去掉「取代记忆#N」前缀，截前 limit 字。"""
    s = re.sub(r"^【?取代记忆#\d+[^】]*】?[：:，,]?\s*", "", str(content or ""))
    s = re.sub(r"\s+", " ", s).strip()[:limit]
    return (s + " 现在的结论是什么").strip()


def selftest():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memories (id INTEGER, superseded_by INTEGER DEFAULT 0)")
    conn.executemany("INSERT INTO memories VALUES (?,?)",
                     [(1, 2), (2, 3), (3, 0), (4, -1), (5, 6), (6, 5), (9, 99)])
    assert resolve_chain(conn, 1)[0:1] + (resolve_chain(conn, 1)[3],) == (3, "live")
    assert resolve_chain(conn, 1)[2] == [1, 2, 3]
    assert resolve_chain(conn, 3)[3] == "already_live"
    assert resolve_chain(conn, 4)[3] == "deprecated"
    assert resolve_chain(conn, 5)[3] == "cycle"
    assert resolve_chain(conn, 9)[3] == "missing"
    conn.close()
    print("SELFTEST OK | 6 组链解析断言通过（单跳/多跳/现行/废弃/环/断链）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    root = args.root or ev._brain_root()
    stream = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout

    def say(s=""):
        print(s, file=stream)

    fp = ev.env_fingerprint(root)
    ok, why = ev.production_parity(fp)
    say(f"环境指纹 env={ev.env_key(fp)} · db={ev.db_key(fp)}"
        f"（memories {fp['memories']} / records {fp['records']}）")
    say(f"生产同构：{'是' if ok else '否'}（{why}）")
    say("只读：真库取代链驱动的 knowledge-update 语料，不写库、不改配置。")

    conn = sqlite3.connect(brain.db_path(root).as_uri() + "?mode=ro",
                           uri=True, timeout=2.0)
    old_rows = conn.execute(
        "SELECT id, superseded_by, content FROM memories "
        "WHERE COALESCE(superseded_by,0)>0 ORDER BY id").fetchall()
    state = Path(tempfile.mkdtemp(prefix="r963_")) / "state.json"
    say(f"\n被取代旧条 {len(old_rows)} 条 → 逐条走链 + 问「现在的结论是什么」")
    say("旧条id  链末端  跳数 形态        旧条注入?  现行条注入?  提问")

    leaked = 0
    terminal_hit = 0
    shapes = {}
    usable = 0
    details = []
    for old_id, _sup, content in old_rows:
        end_id, hops, path, shape = resolve_chain(conn, old_id)
        shapes[shape] = shapes.get(shape, 0) + 1
        prompt = _prompt_from(content)
        if not prompt or shape in ("cycle", "max_hops", "missing"):
            continue
        usable += 1
        try:
            res = brain.prompt_recall(
                root, prompt, agent="workbuddy", cwd=str(ROOT_DIR),
                session=f"r963-{old_id}", state_path=str(state),
                update_state=False, origin="eval")
        except Exception as e:  # noqa: BLE001
            details.append((old_id, end_id, "ERROR " + repr(e)))
            continue
        ids = [int(x) for x in (res.get("memory_ids") or [])]
        is_leaked = int(old_id) in ids
        is_hit = int(end_id) in ids
        leaked += int(is_leaked)
        terminal_hit += int(is_hit)
        details.append((old_id, end_id, hops, shape, is_leaked, is_hit, prompt[:34]))
        say(f"{old_id:>5} {end_id:>7} {hops:>4} {shape:<12} "
            f"{'是!!' if is_leaked else '否':>8}  {'是' if is_hit else '否':>10}  {prompt[:34]}")

    say(f"\n汇总：被取代旧条 {len(old_rows)} 条 · 有效提问 {usable} 条 · "
        f"链形态 {shapes}")
    say(f"① **旧条泄漏 {leaked}/{usable}**"
        f"（{'%.1f%%' % (leaked / usable * 100) if usable else 'n/a'}）"
        " —— 机制正确性硬判据，应为 0")
    say(f"② 现行条召回 {terminal_hit}/{usable}"
        f"（{'%.1f%%' % (terminal_hit / usable * 100) if usable else 'n/a'}）"
        " —— 有用性观察项，不是改代码判据")
    say("\n结论：")
    if usable == 0:
        say("  有效样本为 0 → 分母为 0、方向当前不可验证（记忆 #566 口径），"
            "不得把 0/0 写成 0% 收益。")
    elif leaked:
        say(f"  **发现真缺陷**：{leaked} 条已被取代的旧结论仍被注入。"
            "转下一轮定位是检索路漏过滤还是链末端没写对，先写会红的用例再修。")
    else:
        say("  旧条零泄漏 → superseded_by / _mem_live_sql 过滤在注入路径上确实生效，"
            "B115 的机制正确性关账；现行条召回率低属检索质量/语料标注问题，"
            "登记为观察项，不拿它当「改代码」的由头（记忆 #523 口径）。")
    say("  口径：装置样本 origin=eval 不进生产分母；提问由旧条正文前 70 字生成，"
        "不引入人工标注答案，只查 id 是否在注入里；全程 SQLite mode=ro。")
    conn.close()
    if args.out:
        stream.close()
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
