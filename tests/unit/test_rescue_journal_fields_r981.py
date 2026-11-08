# -*- coding: utf-8 -*-
"""B120 / R981：救援流水补 `replaced` 与 `margin` 两个字段。

为什么：B107 等到首笔 `origin=live` 语义救援（2026-11-08T11:58:05，ids=501），
账上只有「加进来的是谁」，没有「换掉的是谁、两条差多少」——于是无法判断这次替换
是对是错（详见 output/迭代依据-R976-B107首笔live救援复核-2026-11-08.md）。
`replace` 分支内部本来就算了 `worst_i` 与 `best_sim - worst_sim`，只是没往外带。

边界：**零行为影响**，只加流水字段。append 档不换人 → 不该产出 `replaced`；
off 档根本没有流水；margin 必须 ≥ `RECALL_SEM_RESCUE_MARGIN`（低于这个值不换人）。
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))

PROMPT = "麒麟 貔貅 白泽 通用 泛词 怎么修"
TERMS = [("麒麟", 1.0), ("貔貅", 1.0), ("白泽", 1.0),
         ("通用", 1.0), ("泛词", 1.0)]


def _row(i, text, **kw):
    d = {"id": i, "content": text, "tags": "", "kind": "lesson",
         "project": "", "use_count": 0, "adopt_count": 0, "inject_count": 0,
         "pinned": 0}
    d.update(kw)
    return d


def _pool(monkeypatch, strong_ids=(1,), weak_ids=(2, 3, 4, 5, 6, 7, 8), sem=None):
    import brain
    rows = []
    for n, i in enumerate(strong_ids):
        rare = ("麒麟", "貔貅", "白泽")[n % 3]
        rows.append(_row(i, f"{rare} 通用 泛词 独有结论{i}"))
    for i in weak_ids:
        rows.append(_row(i, f"通用 泛词 泛化记录{i}"))
    monkeypatch.setattr(brain, "extract_prompt_terms", lambda *a, **k: list(TERMS))
    monkeypatch.setattr(brain, "search_memories",
                        lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(brain, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_recall_project_block", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_memory_counters", lambda *a, **k: {})
    monkeypatch.setattr(brain, "_semantic_rank",
                        lambda *a, **k: list(sem or []))


def _sem(i, sim):
    return (i, 1.0 / (60 + i), {"id": i, "_semantic_sim": sim})


def _notes(root):
    con = sqlite3.connect(str(Path(root) / "_hub" / "brain.db"))
    try:
        return [r[0] or "" for r in con.execute(
            "SELECT note FROM journal WHERE action='brain_recall_sem_rescue'")]
    finally:
        con.close()


def _recall(hub_root, tmp_path, session="s1"):
    import brain
    return brain.prompt_recall(
        str(hub_root), PROMPT, agent="workbuddy", session=session,
        state_path=str(tmp_path / "state.json"),
        update_state=False, origin="eval")


def test_replace_records_replaced_and_margin(hub_root, tmp_path, monkeypatch):
    """换人时账上必须有「换掉谁 + 差多少」：0.95 vs 0.20 → margin 0.75。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, sem=[_sem(5, 0.95), _sem(1, 0.20)])
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_MODE", "replace")
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_SIM", 0.65)
    out = _recall(hub_root, tmp_path)
    assert out["sem_rescued"] == 1, out
    notes = _notes(hub_root)
    assert notes, "replace 触发却没写流水"
    note = notes[-1]
    assert "replaced=1" in note, note
    assert "margin=0.75" in note, note
    assert "ids=5" in note, note


def test_append_has_no_replaced_field(hub_root, tmp_path, monkeypatch):
    """append 不换人，账上就不许出现 replaced（否则以后归因会被误导）。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, strong_ids=(), sem=[_sem(5, 0.90)])
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_MODE", "append")
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_SIM", 0.65)
    out = _recall(hub_root, tmp_path)
    assert out["sem_rescued"] == 1, out
    note = _notes(hub_root)[-1]
    assert "replaced=" not in note, note
    assert "margin=" not in note, note


def test_margin_below_threshold_writes_nothing(hub_root, tmp_path, monkeypatch):
    """差距不够 → 不换人 → 不写流水（流水只为真实发生的替换留证）。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, sem=[_sem(5, 0.70), _sem(1, 0.66)])
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_MODE", "replace")
    monkeypatch.setattr(brain, "RECALL_SEM_RESCUE_SIM", 0.65)
    out = _recall(hub_root, tmp_path)
    assert out["sem_rescued"] == 0, out
    assert _notes(hub_root) == [], _notes(hub_root)
