# -*- coding: utf-8 -*-
"""R827：注入采纳率报表——「注入的记忆到底被用了几条」。

    python tools/recall_adherence_report.py            # 近 30 天
    python tools/recall_adherence_report.py --days 7
    python tools/recall_adherence_report.py --mark <session> --text "这一轮的产出…"

口径（为什么这么算，别照着改）：
  · **分母只算可指纹行**。「抽不出指纹」不等于「没被用」，那是「测不了」；把它算进
    分母会系统性压低采纳率，从而让人得出「注入没用」的错误结论。
  · 判定分三态（R852）：**未观测**（这行从没被判定过，used 恒为 0，不能算被拒绝）、
    **判无用**（判官带着证据看过、没认出来）、**判用上**（指纹命中）。定罪看观测口径
    used/判过，账面 used/可指纹 里掺着未观测。
  · 40%–70% 那句原先写作「sheratonhq 的实测口径」，R851 收尾取证查无出处（记忆 #523），
    现在**线只有巡检项那一条**（`ops.ADHERENCE_BAD_RATE` 观察线 20% / `ADHERENCE_OK_RATE`
    正常线 40%，均为本库自设）：低于观察线先看排序与门槛，观察区内算「能用但不算宽裕」，
    判过的全命中反而要怀疑注入给得太少或太泛。两处各发一套判词，读的人只能挑一句信。
  · 平均记忆年龄对照 **7–30 天**：太短=只靠近期记忆，太长=旧结论权重过高。
  · 分档看（strong / weak / R833 起的 error＝工具失败那一刻的注入）——如果各档采纳率
    差不多，说明 R826 的「分流」判断不成立；error 档单独看，它面对的是「模型正卡在
    错误上」的时刻，理论上比按句回忆更容易被用上，没差距就要查签名词是不是抽得太泛。

标记采纳（agent 侧调用）：
    --mark <session> --text "<本轮回答或执行的命令>"   → 命中指纹即记 used
"""
import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _console  # noqa: E402,F401  控制台 UTF-8 守卫
import brain  # noqa: E402
import ops  # noqa: E402

# 线只有一套：巡检项说什么，报表就说什么。原来这里是 40/70 而 ops 是 20/40，
# 同一个库对同一个数发两套判词，读的人只能挑一句信（R852 收掉）。
RATE_WATCH = ops.ADHERENCE_BAD_RATE     # 观察线：观测口径低于此，注入精度该收
RATE_OK = ops.ADHERENCE_OK_RATE         # 正常线：以上算正常
AGE_LO, AGE_HI = 7.0, 30.0              # 记忆年龄的经验区间（同样为本库自设）


def _verdict(rate: float, judged: int) -> str:
    """判词按「判过的笔数」给：分母掺了未观测就会把采集缺陷读成「记忆没人用」（R852）。"""
    if judged == 0:
        return "无从判定（还没有判过的可指纹注入记录——先确认回填/补扫有没有在跑）"
    if rate < RATE_WATCH:
        return "偏低——先看排序（Recall@3）与门槛（_recall_rare_threshold），再看注入文本是否太长被忽略"
    if rate < RATE_OK:
        return f"观察区（{RATE_WATCH:.0f}%–{RATE_OK:.0f}%，两线本库自设）——能用但不算宽裕"
    if judged >= 5 and rate >= 100.0:
        # 判过的全都命中，更可能是给得太少/太泛（每条都在上下文里），而不是记忆真的条条有用
        return "全命中——留意注入是否过少或过泛（全是必要条件时，采纳率失去区分力）"
    return "在本库正常线以上"


EMPTY_FP_WHERE = "fp is null or trim(fp) in ('','[]')"


def explain_empty(root: str, days: int = 30) -> dict:
    """R938/B088：把「判不了」按来历(origin)与成因(fp_reason)分栏，只读。

    不回填历史行：R834 锚点教训——跨口径变更点的比率不可比；采集上线前的
    空值单列 `legacy`，不冒充新口径。装置行(eval/probe)与生产行(live/brief)
    必须分开报（大脑记忆 #581），拿总数谈优先级会把装置噪声当需求。
    """
    cutoff = brain._days_ago(days)
    by_origin, by_reason, by_strength = {}, {}, {}
    prod = dev = 0
    # R964/B042：第三维「档位」。R950 复评时 error 档只按总数挂着，看不出它
    # 到底有没有被判、判不了是生产行还是装置行。按 (origin, reason, strength)
    # 三键分组，一次查询同时喂三张表，不给钩子/报表路径加固定成本。
    with brain.db_conn(root) as conn:
        rows = conn.execute(
            "SELECT origin, fp_reason, COALESCE(strength,'(blank)'), count(*) "
            "FROM recall_adherence "
            f"WHERE {EMPTY_FP_WHERE} AND ts >= ? GROUP BY 1,2,3", (cutoff,)).fetchall()
    for origin, reason, strength, n in rows:
        o = origin or "(blank)"
        r = reason or "legacy"
        s = strength or "(blank)"
        by_origin[o] = by_origin.get(o, 0) + n
        by_reason[r] = by_reason.get(r, 0) + n
        b = by_strength.setdefault(
            s, {"total": 0, "production": 0, "device": 0, "by_reason": {}})
        b["total"] += n
        b["by_reason"][r] = b["by_reason"].get(r, 0) + n
        if o in brain.PRODUCTION_ORIGINS:
            prod += n
            b["production"] += n
        elif o in brain.DEVICE_ORIGINS:
            dev += n
            b["device"] += n
    return {"by_origin": by_origin, "by_reason": by_reason,
            "by_strength": by_strength,
            "production": prod, "device": dev, "total": prod + dev}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=brain.ADHERENCE_KEEP_DAYS)
    ap.add_argument("--mark", default="", help="要标记采纳的 session")
    ap.add_argument("--text", default="", help="这一轮的产出文本（用来匹配指纹）")
    ap.add_argument("--root", default="")
    ap.add_argument("--explain-empty", action="store_true",
                    help="R938：按来历/成因分栏报出「判不了」的注入行（只读）")
    a = ap.parse_args()
    root = a.root or _root()
    if not root:
        print("× 找不到大脑根：设 AGENTHUB_ROOT 或 ~/.agenthub/config.json")
        return 2
    # 报表脚本跑在 MCP 服务进程之外，老库可能还没补过列（2026-11-01 实测直接
    # OperationalError: no such column: inject_count）。init_db 幂等，先补schema再查。
    brain.init_db(root)

    if a.explain_empty:
        e = explain_empty(root, a.days)
        print("空指纹注入行（按来历 / 成因分栏，只读，不回填历史行）")
        print(f"  生产 {e['production']} · 装置 {e['device']} · 合计 {e['total']}")
        print(f"  按来历：{e['by_origin']}")
        print(f"  按成因：{e['by_reason']}  （legacy = 采集上线前的历史行）")
        # R964/B042：档位分栏。error 样本不足只报数，不对该档优劣下结论
        # （30 笔判过是本库自设的最小观察线，见 by_directive 段）。
        print(f"  按档位：{dict((k, v['total']) for k, v in e['by_strength'].items())}")
        for name, b in sorted(e["by_strength"].items(),
                              key=lambda kv: -kv[1]["total"]):
            print(f"    {name:<10} 共 {b['total']:>4}"
                  f"（生产 {b['production']} · 装置 {b['device']}）"
                  f"  成因 {b['by_reason']}")
        eb = e["by_strength"].get("error", {}).get("total", 0)
        if eb < 30:
            print(f"  error 档判不了仅 {eb} 条（观察线 30 笔）：只报数，"
                  "不对该档优劣下结论（B042 仍待样本）")
        return 0

    if a.mark:
        if not a.text:
            print("× --mark 需要同时给 --text（这一轮的回答或命令）")
            return 2
        r = brain.mark_recall_used(root, a.mark, a.text)
        dev = f" · 装置行 {r['device_matched']} 条（只判定，不进 adopt_count）" \
            if r.get("device_matched") else ""
        print(f"检查 {r['checked']} 条注入记录，命中 {r['matched']} 条：{r['memory_ids']}{dev}")
        return 0

    st = brain.recall_adherence_stats(root, a.days)
    print(f"窗口：近 {st['window_days']} 天（大脑根：{root}）")
    print(f"注入记录  {st['injected']} 条（生产 origin=live 逐句回忆 + brief 简报规则）"
          + (f" · 另有 {st['device_rows']} 条是 eval/probe 装置写的，已从下面所有账里排除"
             if st.get("device_rows") else ""))
    print(f"可指纹    {st['fingerprintable']} 条"
          + (f"（占 {st['fingerprintable'] / st['injected'] * 100:.0f}%）" if st["injected"] else ""))
    print(f"被用      {st['used']} 条")
    print(f"三态      判不了 {st.get('unjudgeable', 0)} · 未观测 {st.get('unobserved', 0)} · "
          f"判无用 {st.get('judged_unused', 0)} · 判用上 {st.get('judged_used', 0)}")
    if st.get("unobserved"):
        tail = int(st.get("unobserved_tail") or 0)
        gap = int(st.get("unobserved_gap") or 0)
        old = int(st.get("unobserved_gap_old") or 0)
        cur = max(0, gap - old)
        start = str(st.get("capture_start") or "")
        hint = "尾巴是结构性上限（证据要等下一句），断档才是采集漏了"
        if cur:
            hint += "——先看对应宿主有没有 Stop/补扫（log: ~/.agenthub/logs/brain-adherence.log）"
        print(f"  未观测拆解  尾巴 {tail}（会话最后一笔，等不到下一句）· "
              f"断档 {gap}（历史 {old} · 现行 {cur}）· {hint}")
        if old:
            where = f"（采集口径起点 {start}）" if start else ""
            print(f"    历史断档早于首条带 src 的采集{where}，证据已不可回溯；"
                  f"现行断档 {cur} 才是要查的采集缺陷")
    obs = float(st.get("observed_rate") or 0.0)
    print(f"采纳率    观测口径 {obs}%（判过 {st.get('judged', 0)} 笔）· 账面 {st['rate']}%"
          f"（可指纹 {st['fingerprintable']} 笔）→ {_verdict(obs, int(st.get('judged') or 0))}")
    if st["avg_memory_age_days"] is not None:
        age = st["avg_memory_age_days"]
        note = "偏新（只靠近期记忆）" if age < AGE_LO else (
            "偏旧（旧结论权重可能过高）" if age > AGE_HI else "在区间内")
        print(f"平均记忆年龄 {age} 天（参考 {AGE_LO:.0f}–{AGE_HI:.0f}）→ {note}")
    if st["by_strength"]:
        # 档位从账本里动态取（R833 起多了 error 一档：工具失败那一刻的注入）。
        # 硬编码 strong/weak 会让新档悄悄掉出报表——看了半天以为没人用它，其实只是没印。
        print("\n分档（R826 的分流判断是否成立，看各档采纳率差距）：")
        label = {"strong": "strong 强命中", "weak": "weak 弱命中",
                 "error": "error 报错即时"}
        for s, b in sorted(st["by_strength"].items(),
                           key=lambda kv: -kv[1]["injected"]):
            print(f"  {label.get(s, s):<14} 注入 {b['injected']:>4}  "
                  f"可指纹 {b['fingerprintable']:>4}  被用 {b['used']:>3}  "
                  f"采纳率 {b['rate']}%")
        if "error" in st["by_strength"]:
            e = st["by_strength"]["error"]
            if e["injected"] and not e["fingerprintable"]:
                print("  （error 档还抽不出指纹——先别按采纳率判它好坏，先看送达数：cli.py doctor --only brief_hook_health）")

    # R881/B089：R879 的策略层到底有没有用，靠这两档对比说话。样本不足只报数，
    # 不许下结论——30 笔判过是能看清差距的最小观察线（本库自设，非外部区间）。
    if st.get("by_directive"):
        print("\n策略档位（R879：标【必须照做】的到底更听不听；样本不足时只报数）：")
        label = {"required": "required 必须照做", "supportive": "supportive 背景参考",
                 "unknown": "unknown 旧行无档位"}
        for t, b in sorted(st["by_directive"].items(),
                           key=lambda kv: -kv[1]["injected"]):
            print(f"  {label.get(t, t):<20} 注入 {b['injected']:>4}  "
                  f"可指纹 {b['fingerprintable']:>4}  判过 {b['judged']:>4}  "
                  f"被用 {b['used']:>3}  账面 {b['rate']}%  观测 {b['observed_rate']}%")
        req = st["by_directive"].get("required", {}).get("judged", 0)
        sup = st["by_directive"].get("supportive", {}).get("judged", 0)
        if req < 30 or sup < 30:
            print(f"  样本不足（判过 required {req} / supportive {sup}，观察线 30 笔）："
                  "只报数，不对两档优劣下结论（B089 待确认）")

    # R829：曝光与采纳是两本账。只看采纳率回答不了「该动谁」——这两张榜才是动作清单。
    ev = brain.exposure_vs_adoption(root)
    print(f"\n全库累计：曝光 {ev['inject_total']} 次 · 证实采纳 {ev['adopt_total']} 次")
    if ev["most_adopted"]:
        print("真被用上的（该升）：")
        for m in ev["most_adopted"]:
            print(f"  #{m['id']:<4} 采 {m['adopt_count']:<3} 推 {m['inject_count']:<4} "
                  f"[{m['kind'] or 'note'}] {str(m['project'] or '全局')[:20]}")
    else:
        print("真被用上的：还没有一条——采纳率账本还没跑起来，先确认 Stop 回填钩子在位"
              "（cli.py doctor --only brief_hook_health）")
    if ev["most_injected_never_adopted"]:
        print("反复推送却从没被用上的（降权候选，继续推只会让模型对整块注入免疫）：")
        for m in ev["most_injected_never_adopted"][:8]:
            body = str(m.get("head") or "")[:46].replace("\n", " ")
            mark = "⏸ 已冷却" if m.get("muted") else "  待观察"
            print(f"  #{m['id']:<4} 推 {m['inject_count']:<4} 采 0  {mark}  "
                  f"[{m['kind'] or 'note'}] {body}")
    # R830：光列清单没用——断路器有没有真的在挡，要能一眼看见「现在挡了几条」。
    print(f"\n免疫断路器（推 ≥5 次零采纳 + 30 天内）：冷却中 {ev['muted_total']} 条"
          "（主动检索不受影响，30 天后自动放行一次）")
    for m in ev["muted"][:6]:
        pin = " ·置顶" if m.get("pinned") else ""
        body = str(m.get("head") or "")[:40].replace("\n", " ")
        print(f"  ⏸ #{m['id']:<4} 推 {m['inject_count']:<4}{pin}  [{m['kind'] or 'note'}] {body}")
    return 0


def _root() -> str:
    import json
    import os
    env = os.environ.get("AGENTHUB_ROOT")
    if env:
        return env
    cfg = Path.home() / ".agenthub" / "config.json"
    try:
        return str(json.loads(cfg.read_text(encoding="utf-8")).get("root") or "")
    except (OSError, ValueError):
        return ""


if __name__ == "__main__":
    raise SystemExit(main())
