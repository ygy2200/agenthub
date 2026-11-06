# -*- coding: utf-8 -*-
"""R924：简报到底哪一段被截走——按**渲染后**的分段字数取证（只读）。

为什么要单独测一轮：R922 探针报「规则段 3878 字 > 整份预算 3600（占 108%）」是**错的口径**——
它拿的是 memories.content 的原始字数总和，而 `session_brief` 里每条规则走
`_brief_rule_line(content, 120)`，渲染出来只有 8×~130 字。拿着这个错数字直接去做「分段子预算」
就是给一个不存在的问题开药（R833 的老教训：口径错了，后面全错）。

判据写在前面（跑之前定，跑完不挑口径）：
  · 生产送达形状（从 brain_brief 流水里取真实 agent×project 组合）中，只要有段落被
    **整段**截走，就说明单一全局尾巴不够用 → 分段子预算成立，动手；
  · 一个都没有就是 budget_ok：预算问题不在分段，别动渲染。
  · 顺带量出「规则段实占多少字」——这才是 R922 那条 108% 该问的问题。

用法：python tools/brief_layout_probe_r924.py [--json-out 证据文件]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  Windows 管道默认 cp936，▸/· 会 UnicodeEncodeError
import brain  # noqa: E402
import core  # noqa: E402

BUDGET = brain.BRIEF_MAX_CHARS
# 渲染里的切片口径（brain.session_brief 尾部）：text[:max_chars-24] + 截断标记
TAIL_MARK = 24
CUT_MIN_SECTIONS = 2
AGENTS = ("qoder", "workbuddy", "zcode", "hermes", "DSH")


def split_sections(text: str) -> list:
    """按「▸ 」切段，返回 [{name, start, end, chars}]，段前头部归到「(抬头)」。"""
    out, pos, cur = [], 0, None
    for line in text.split("\n"):
        if line.startswith("▸ "):
            if cur is not None:
                cur["end"] = pos
                out.append(cur)
            cur = {"name": line[2:].split("（")[0].strip(), "start": pos}
        pos += len(line) + 1
    if cur is not None:
        cur["end"] = pos
        out.append(cur)
    # 最后一行没有换行符，循环里的 `pos += len(line) + 1` 会多数 1 个字：
    # 不封顶的话最后一段永远被误判成「截半」（12/12 形状都中招，差点当成缺陷开工）。
    for s in out:
        s["end"] = min(s["end"], len(text))
    for s in out:
        s["chars"] = s["end"] - s["start"]
    return out


def classify(sec: dict, kept_to: int) -> str:
    if sec["start"] >= kept_to:
        return "cut"          # 整段没送出去
    if sec["end"] > kept_to:
        return "partial"      # 送了一半
    return "full"


def production_shapes(root: str, days: int) -> list:
    """从流水里取真实送达过的 (agent, project) 组合——不自己编形状（R833 口径）。

    `brain_brief` 的流水 note 形如 `2067 chars · project=AgentHub-多agent文件统一管理 · rules=8`，
    项目名是送达现场的原话；再补一个 project=- 的形状（cwd 没落在项目目录时走推断分支）。
    """
    import re
    with brain.db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT agent, note FROM journal WHERE action='brain_brief'"
            " ORDER BY ts DESC LIMIT 400")]
    shapes, seen = [], set()
    for r in rows:
        agent = (r.get("agent") or "").strip()
        if not agent or agent == "unknown":
            continue
        m = re.search(r"project=([^\s·]+)", r.get("note") or "")
        proj = (m.group(1) if m else "-").strip()
        key = (agent, "" if proj == "-" else proj)
        if key in seen:
            continue
        seen.add(key)
        shapes.append({"agent": agent, "project": key[1]})
    return shapes[:12]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="简报分段预算取证（只读）")
    ap.add_argument("--root", default="")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json-out", default="", help="把完整分段账写到这个文件（证据用）")
    args = ap.parse_args(argv)
    root = (args.root or (core.load_config() or {}).get("root", "") or "").strip()
    if not root:
        print("读不到大脑根目录")
        return 1

    print(f"简报预算 {BUDGET} 字 · 判据：真实送达形状里出现「整段被截走」即动分段预算"
          f"（现场证据：本会话简报尾巴停在「置顶记忆」第二条）\n")
    shapes = production_shapes(root, args.days)
    report = []
    worst = {}
    for sh in shapes:
        big = brain.session_brief(root, agent=sh["agent"], project=sh["project"],
                                  max_chars=200000)
        text = big.get("text") or ""
        if not text:
            continue
        secs = split_sections(text)
        cut_at = min(len(text), BUDGET - TAIL_MARK)
        state = {"name": f"{sh['agent']}|{sh['project'] or '项目外(推断)'}",
                 "total": len(text), "over_budget": len(text) > BUDGET,
                 "rules_rendered": 0, "sections": []}
        for s in secs:
            st = classify(s, cut_at)
            state["sections"].append({"name": s["name"], "chars": s["chars"],
                                      "at": s["start"], "state": st})
            if st in ("cut", "partial"):
                worst.setdefault(s["name"], []).append(st)
            if s["name"].startswith("必做规则"):
                state["rules_rendered"] = s["chars"]
        report.append(state)
        cut_names = [x["name"] for x in state["sections"] if x["state"] == "cut"]
        part = [x["name"] for x in state["sections"] if x["state"] == "partial"]
        print(f"[{state['name']}] 渲染 {state['total']} 字 · 超预算={state['over_budget']}"
              f" · 规则段实占 {state['rules_rendered']}"
              f" · 整段截走 {len(cut_names)} 段 {cut_names} · 截半 {part}")

    n_cut_shapes = sum(1 for r in report
                       if any(x["state"] == "cut" for x in r["sections"]))
    victims = sorted(worst.items(), key=lambda kv: -len(kv[1]))
    print(f"\n送达形状 {len(report)} 个 · 有整段截走的是 {n_cut_shapes} 个")
    print("被截段落频次：" + "、".join(f"{k}×{len(v)}" for k, v in victims[:8]))
    verdict = "needs_sub_budget" if (n_cut_shapes and victims) else "budget_ok"
    print(f"\n判定：{verdict}（判据写在前：生产形状里有段落被整段截走 → 分段预算成立）")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"budget": BUDGET, "verdict": verdict, "cut_shapes": n_cut_shapes,
                       "shapes": report}, f, ensure_ascii=False, indent=1)
        print(f"完整分段账 -> {args.json_out}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
