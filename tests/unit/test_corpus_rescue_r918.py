# -*- coding: utf-8 -*-
"""R918：全库稀有度救援的四条护栏。

① 默认 off；② 只在 picked 全空（gap）时补；③ 最多 1 条；④ 准入信号是全库 df
出现率，不是 R907 那个已被证伪的「多词共现」启发式。读不了全库统计要 fail-open。
旧码无 RECALL_CORPUS_RESCUE 与 corpus_rescued 字段，本文件在旧码上必红。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import brain  # noqa: E402

PROMPT = "zetaquux 怎么用"


def _recall(hub_root, tmp_path, monkeypatch, mode="off", ratio=0.02,
            df_map=None, n_total=100, force_weak=True, token_stats=None):
    brain.init_db(str(hub_root))
    rows = [{"id": 1, "content": "zetaquux 的用法说明", "tags": "zetaquux",
             "kind": "lesson", "status": "active"},
            {"id": 2, "content": "zetaquux 的另一条", "tags": "zetaquux",
             "kind": "lesson", "status": "active"}]
    monkeypatch.setattr(brain, "search_memories", lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_semantic_rescue", lambda *a, **k: [])
    if force_weak:
        monkeypatch.setattr(brain, "_recall_rare_threshold", lambda n: 999.0)
    if df_map is None:
        df_map = {"zetaquux": 1}
    if token_stats is None:
        token_stats = lambda root, tokens: ({t: df_map.get(t, n_total)  # noqa: E731
                                            for t in tokens}, n_total)
    monkeypatch.setattr(brain, "_token_stats", token_stats)
    brain.RECALL_CORPUS_RESCUE = mode
    brain.RECALL_CORPUS_RARE_RATIO = ratio
    try:
        return brain.prompt_recall(
            str(hub_root), PROMPT, agent="workbuddy", session="unit",
            state_path=str(tmp_path / "state.json"),
            update_state=False, origin="eval")
    finally:
        brain.RECALL_CORPUS_RESCUE = "off"
        brain.RECALL_CORPUS_RARE_RATIO = 0.02


def test_default_off_and_observable_fields(hub_root, tmp_path, monkeypatch):
    out = _recall(hub_root, tmp_path, monkeypatch)
    assert out["corpus_rescued"] == 0 and out["corpus_rescued_id"] == 0, out
    assert brain.RECALL_CORPUS_RESCUE == "off"


def test_corpus_rare_term_rescues_exactly_one(hub_root, tmp_path, monkeypatch):
    out = _recall(hub_root, tmp_path, monkeypatch, mode="gap",
                  df_map={"zetaquux": 1}, n_total=100)
    assert out["corpus_rescued"] == 1, out
    assert out["corpus_rescued_id"] in (1, 2), out
    assert len(out["memory_ids"]) == 1, out["memory_ids"]


def test_globally_common_term_does_not_rescue(hub_root, tmp_path, monkeypatch):
    """全库出现率 30% → 闸门判对了，不许放行。"""
    out = _recall(hub_root, tmp_path, monkeypatch, mode="gap",
                  df_map={"zetaquux": 30}, n_total=100)
    assert out["corpus_rescued"] == 0, out
    assert not out["memory_ids"], out["memory_ids"]


def test_strong_candidates_are_never_displaced(hub_root, tmp_path, monkeypatch):
    """picked 非空时整段不跑，绝不挤掉强命中。"""
    out = _recall(hub_root, tmp_path, monkeypatch, mode="gap",
                  df_map={"zetaquux": 1}, n_total=100, force_weak=False)
    assert out["corpus_rescued"] == 0, out


def test_unavailable_corpus_stats_fail_open(hub_root, tmp_path, monkeypatch):
    """全库统计读不到就整段跳过，不许影响即时回忆。"""
    def boom(*a, **k):
        raise RuntimeError("db busy")

    out = _recall(hub_root, tmp_path, monkeypatch, mode="gap", df_map={},
                  token_stats=boom)
    assert out["corpus_rescued"] == 0, out
    assert out["memory_ids"] == [], out["memory_ids"]


def test_ab_tool_has_guard_and_restores_both_constants():
    src = (ROOT / "tools" / "corpus_rescue_ab_r918.py").read_text(encoding="utf-8")
    assert "env_guard_line" in src
    assert "brain.RECALL_CORPUS_RESCUE, brain.RECALL_CORPUS_RARE_RATIO = saved" in src
