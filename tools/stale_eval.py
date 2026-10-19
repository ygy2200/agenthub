# -*- coding: utf-8 -*-
"""D3/R3：过期值服务率 harness（stale-value serving rate）——记忆系统的「诚实指标」。

为什么做这个（外部研究一致结论）：
  · 「persistence ≠ truth」——记忆系统最常见的生产失效不是「忘了」，是**旧事实安静地
    变假**（TimeQA：最佳模型 46% vs 人类 87%；SituatedQA：六分之一问题的答案随时间变）。
  · ranksquire 2026：「架构评审第一个该看的数字是 **staleness rate** 而不是 benchmark 分数」。
  · MemoryAgentBench：冲突解析最强系统仅 54%——系统不是找不到记忆，是找到两版后选错。

本 harness 用**合成标注集**（每类事实的当前真相已知）测四件事：
  ① **supersede 泄漏率**：已标 superseded_by 的旧事实是否仍被检索出来
     （_live_sql 应结构性排除；任何泄漏=检索管线回归。期望 0%）；
  ② **ghost 混合率**：无取代关系的高相似两版共存时，一次查询是否**同时**返回两版
     （= 研究说的「从混合物里综合」；这是真实失效面，期望低但通常非 0）；
  ③ **recency 排序正确率**：ghost 对里**较新**那版是否排在较旧之前
     （D5「确定性裁决」的基线：LLM 识别、代码按 serial 选——先量现状）；
  ④ **静默过期可检出率**：无后继版本的旧事实（「安静变假」类）是否被任何**陈旧信号**
     标出（valid_until 过期 / 超过复核期）——理论上检索层无法知道真相，可行动的是
     「标出来让人复核」，本项量的是**这件事做了没有**。

用法：python tools/stale_eval.py [--keep]
退出码：① 泄漏率 > 0 → 非零（硬不变量）；其余只报告不断言（度量类指标）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

import brain  # noqa: E402

# 每类事实的合成数据：同一主题词根，两版内容只差版本值（模拟真实「事实变更」）
TOPICS = [
    ("代理端口", "7890", "7891"),
    ("备份目录", "D:/backup-old", "D:/backup-new"),
    ("Python 路径", "D:/python39/python.exe", "D:/python311/python.exe"),
    ("数据库路径", "D:/old/brain.db", "D:/new/brain.db"),
    ("服务端口", "8787", "8788"),
    ("模型缓存目录", "C:/cache-old", "D:/cache-new"),
    ("工作区盘符", "E:/workspace", "D:/workspace"),
    ("代理模式", "全局模式", "规则模式"),
    ("截图目录", "C:/shots-old", "D:/shots"),
    ("日志级别", "DEBUG", "INFO"),
    ("构建目录", "D:/build-old", "D:/build"),
    ("字体路径", "C:/fonts-old", "C:/Windows/Fonts"),
    ("Node 路径", "D:/node18/node.exe", "D:/node/node.exe"),
    ("缓存开关", "缓存已关闭", "缓存已开启"),
    ("数据盘", "F:/data", "D:/data"),
    ("API 端点", "http://127.0.0.1:8000", "http://127.0.0.1:8001"),
    ("同步间隔", "每 5 分钟同步一次", "每 30 分钟同步一次"),
    ("超时设置", "请求超时 10 秒", "请求超时 60 秒"),
]
STABLE = [
    "本机是 Win10 26100",
    "大脑数据根在 D:/agent资料/zcode项目记录",
    "六个 agent 全部接入 AgentHub",
    "回归共 11 套对抗套件",
    "零第三方依赖是硬约束",
    "MCP 走 stdio 传输",
]


def build_eval_hub() -> tuple[str, dict]:
    """合成标注集：10 组三形态（superseded 对 / ghost 对 / 静默过期单条）+ 6 条稳定对照。
    返回 (root, 标注信息)。用独立的 tmp 根，绝不碰真实库。"""
    tmp = Path(tempfile.mkdtemp(prefix="stale_eval_")) / "hub"
    tmp.mkdir(parents=True)
    brain.init_db(str(tmp))
    root = str(tmp)
    annotation = {"superseded": [], "ghost": [], "silent": []}
    for i, (topic, old_v, new_v) in enumerate(TOPICS):
        # 三形态按 i 轮转：i%3==0 superseded / 1 ghost / 2 silent
        form = ("superseded", "ghost", "silent")[i % 3]
        old_txt = f"环境事实：{topic}是 {old_v}（{topic} 本机配置）"
        old_id = brain.add_memory(root, old_txt, kind="fact", agent="zcode",
                                  project="陈旧评估", tags=topic)
        if form in ("superseded", "ghost"):
            new_id = brain.add_memory(root, f"环境事实：{topic}是 {new_v}（{topic} 本机配置）",
                                      kind="fact", agent="zcode", project="陈旧评估", tags=topic)
            if form == "superseded":
                with brain.db_conn(root) as conn:
                    conn.execute("UPDATE memories SET superseded_by=? WHERE id=?", (new_id, old_id))
                annotation["superseded"].append({"topic": topic, "old": old_id, "new": new_id})
            else:
                annotation["ghost"].append({"topic": topic, "old": old_id, "new": new_id})
        else:
            annotation["silent"].append({"topic": topic, "old": old_id, "old_v": old_v})
    for s in STABLE:
        brain.add_memory(root, s, kind="fact", agent="zcode", project="陈旧评估")
    return root, annotation


def _top_ids(root: str, query: str, limit: int = 5) -> list:
    return [r["id"] for r in brain.search_memories(root, query, "", limit, count_hits=False)]


def measure(root: str, ann: dict) -> dict:
    res = {"leak": [], "mixture": [], "recency_ok": 0, "recency_total": 0,
           "silent_flagged": 0, "silent_total": 0, "sugg_ok": 0, "sugg_total": 0}
    # ① supersede 泄漏：查询命中旧版（已被取代）即泄漏
    for p in ann["superseded"]:
        ids = _top_ids(root, p["topic"])
        if p["old"] in ids:
            res["leak"].append(p)
    # ② ghost 混合 + ③ recency 排序
    for p in ann["ghost"]:
        ids = _top_ids(root, p["topic"])
        if p["old"] in ids and p["new"] in ids:
            res["mixture"].append(p)
        if p["old"] in ids or p["new"] in ids:
            res["recency_total"] += 1
            ro = ids.index(p["old"]) if p["old"] in ids else 99
            rn = ids.index(p["new"]) if p["new"] in ids else 99
            if rn < ro:
                res["recency_ok"] += 1
    # ④ 静默过期：有没有任何陈旧信号（valid_until 已过期）
    with brain.db_conn(root) as conn:
        for p in ann["silent"]:
            res["silent_total"] += 1
            row = conn.execute("SELECT valid_until FROM memories WHERE id=?", (p["old"],)).fetchone()
            if row and (row["valid_until"] or "") and row["valid_until"] <= brain._now()[:10]:
                res["silent_flagged"] += 1
    # ⑤ 裁决建议正确率（D5）：ghost 对必须被检出，且建议留「较新的那条」。
    #    这一步是确定性计算的，所以基线应当是 **100%**（不是统计数字）——任何非 100%
    #    都意味着 _adjudicate 的规则或其信号接线错了，跟数据分布无关。
    sugg_map = {frozenset((p["a"], p["b"])): (p.get("suggestion") or {})
                for p in brain.ghost_memory_candidates(root, threshold=0.55, limit=100)}
    for p in ann["ghost"]:
        s = sugg_map.get(frozenset((p["old"], p["new"])))
        if s:
            res["sugg_total"] += 1
            if s.get("keep") == p["new"] and s.get("drop") == p["old"]:
                res["sugg_ok"] += 1
    return res


def main() -> int:
    keep = "--keep" in sys.argv
    root, ann = build_eval_hub()
    res = measure(root, ann)
    n_sup = len(ann["superseded"])
    n_ghost = len(ann["ghost"])
    leak_rate = len(res["leak"]) / max(1, n_sup)
    mix_rate = len(res["mixture"]) / max(1, n_ghost)
    rec_rate = res["recency_ok"] / max(1, res["recency_total"])
    flag_rate = res["silent_flagged"] / max(1, res["silent_total"])
    sugg_rate = res["sugg_ok"] / max(1, res["sugg_total"])
    print("=== 过期值服务率（stale-value serving rate）===")
    print(f"① supersede 泄漏率：{len(res['leak'])}/{n_sup} = {leak_rate:.0%}"
          f"（硬不变量，期望 0%）" + ("" if not res["leak"] else f"  ← 泄漏：{res['leak']}"))
    print(f"② ghost 混合率：{len(res['mixture'])}/{n_ghost} = {mix_rate:.0%}"
          "（一次查询同时返回两版 = 从混合物里综合）")
    print(f"③ recency 排序正确率：{res['recency_ok']}/{res['recency_total']} = {rec_rate:.0%}"
          "（ghost 对里较新版排前——D5 确定性裁决的基线）")
    print(f"④ 静默过期可检出率：{res['silent_flagged']}/{res['silent_total']} = {flag_rate:.0%}"
          "（无后继旧事实被陈旧信号标出的比例——期望被标，现状预计 0%：这正是待补的能力）")
    print(f"⑤ 裁决建议正确率（D5）：{res['sugg_ok']}/{res['sugg_total']} = {sugg_rate:.0%}"
          "（ghost 对的建议是否选中较新版——确定性计算，应当恒等于 100%，非统计数字）")
    if not keep:
        import shutil
        shutil.rmtree(Path(root).parent, ignore_errors=True)
    return 1 if (res["leak"] or res["sugg_ok"] != res["sugg_total"]) else 0


if __name__ == "__main__":
    sys.exit(main())
