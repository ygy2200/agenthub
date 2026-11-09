# -*- coding: utf-8 -*-
"""R1074：RRF 名次融合的**条件启用**（钩子侧最终排序）。

R1073 已量出 RRF 能提召回（+5.8pp）但噪声也涨（+17 格），加相对强度线后仍差一格。
R1074 换的是**触发条件**：只有「词法头名本身语义上站不住」时才动排序。

真库逐例依据：
  · #10 词法头名 mem#563 语义 0.00 → 该动（RRF 把答案捞进 top3）
  · #23 词法头名 mem#489 语义 0.59 → 该动
  · #13 词法头名 mem#132 语义 0.75（语义也认它）→ 不该动，RRF 会把语义第 1 的 #518 挤掉

同库 A/B（53 条、分母 52、快照隔离）：gate 0.70 → +3.9pp / 噪声 −3，故默认档取
K=60 / M=0.6 / GATE=0.70。置 K=0 即逐位回滚到 R1072 行为。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import brain  # noqa: E402


def _row(mid, text, sim=0.0, **kw):
    row = {"id": mid, "content": text, "tags": "", "kind": "lesson",
           "project": "", "use_count": 0, "adopt_count": 0,
           "inject_count": 0, "pinned": 0}
    row.update(kw)
    if sim:
        row["_semantic_sim"] = sim
    return row


TERMS = [("模组", 1.0), ("管理器", 1.0), ("搬运", 1.0), ("迁移", 1.0),
         ("骨架", 1.0), ("引擎", 1.0), ("新项目", 1.0), ("复用", 1.0)]
RICH = "模组 管理器 搬运 迁移 骨架 引擎 新项目 复用"     # 命中全部 8 个词
POOR = "模组 管理器 结论"                              # 只命中 2 个词（够过 need=2）


def _patch(monkeypatch, mems, sem_rows):
    terms = list(TERMS)
    monkeypatch.setattr(brain, "extract_prompt_terms", lambda *a, **k: list(terms))
    monkeypatch.setattr(brain, "search_memories", lambda *a, **k: [dict(r) for r in mems])
    monkeypatch.setattr(brain, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_recall_project_block", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_memory_counters", lambda *a, **k: {})
    monkeypatch.setattr(brain, "_memory_dates", lambda *a, **k: {})
    monkeypatch.setattr(brain, "_recall_rare_threshold", lambda *a, **k: -1.0)
    monkeypatch.setattr(brain, "RECALL_REL_MARGIN", 0.0)
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: list(sem_rows))


def _recall(tmp_path, tag):
    return brain.prompt_recall(
        str(tmp_path), "模组 管理器 怎么搬", agent="workbuddy", session=f"s-{tag}",
        state_path=str(tmp_path / f"{tag}.json"), update_state=False, origin="eval")


def test_默认档为_60_06_070():
    assert brain.RECALL_RRF_K == 60
    assert brain.RECALL_RRF_MARGIN == 0.6
    assert brain.RECALL_RRF_GATE_SIM == 0.70


def test_词法头名语义够高时不启用RRF(monkeypatch, tmp_path):
    """#13 那类：词法头名语义 0.75 → 两条路一致，不许动排序。"""
    # 词法头名（9）命中 8 个词、语义 0.75；另一条只命中 2 个词、语义 0.76。
    # 不启用 RRF 时 9 靠词法分领先；启用 RRF 会让 5 上来——正是 #13 的反例形态。
    top = _row(9, RICH, sim=0.75)
    other = _row(5, POOR, sim=0.76)
    _patch(monkeypatch, [top, other], [(5, 0.05, other), (9, 0.90, top)])
    res = _recall(tmp_path, "gated")
    assert res["rrf_used"] == 0, res
    assert res["memory_ids"][0] == 9, res       # 词法头名保持第一


def test_词法头名语义站不住时启用RRF并改序(monkeypatch, tmp_path):
    """#10/#23 那类：头名语义 0.0 → 让语义第 1 的候选上来。"""
    top = _row(9, RICH, sim=0.0)
    other = _row(5, POOR, sim=0.76)
    _patch(monkeypatch, [top, other], [(5, 0.05, other)])
    res = _recall(tmp_path, "on")
    assert res["rrf_used"] == 1, res
    assert res["memory_ids"][0] == 5, res       # 语义第 1 的乙被 RRF 顶上来


def test_关掉K即逐位回滚(monkeypatch, tmp_path):
    top = _row(9, RICH, sim=0.0)
    other = _row(5, POOR, sim=0.76)
    _patch(monkeypatch, [top, other], [(5, 0.05, other)])
    monkeypatch.setattr(brain, "RECALL_RRF_K", 0)
    res = _recall(tmp_path, "off")
    assert res["rrf_used"] == 0
    assert res["memory_ids"][0] == 9, res       # 回到纯词法顺序
