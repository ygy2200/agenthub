# -*- coding: utf-8 -*-
"""R901：把 R900 的 A/B 胜出档合入默认，并给救援留 origin 分档观测出口。

为什么要有这份用例：R900 量出 replace/0.60–0.62 平台段 +9.6pp、2/2 精准、
零丢失、噪声 -2，但当时默认仍是 off。合入默认属于会改线上注入行为的实质变更，
必须有能咬的断言钉住「用的是哪一档」「护栏有没有被顺手放宽」。

第二半是观测：默认档上线后如果没人知道真实会话里触发几次，下一轮又会退回靠猜。
流水 note 里的 origin 必须区分 live/probe/eval（B077 半解 + 记忆 #566：probe/eval
的装置流量不能冒充线上收益）；note 不许写进 backup（记忆 #569：第 5 个位置参数是
backup 不是 note）。
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "tools"))

PROMPT = "麒麟 貔貅 白泽 通用 泛词 怎么修"
TERMS = [("麒麟", 1.0), ("貔貅", 1.0), ("白泽", 1.0),
         ("通用", 1.0), ("泛词", 1.0)]


def _row(i, text):
    return {"id": i, "content": text, "tags": "", "kind": "lesson",
            "project": "", "use_count": 0, "adopt_count": 0,
            "inject_count": 0, "pinned": 0}


def _pool(monkeypatch, sem):
    import brain
    rows = [_row(1, "麒麟 通用 泛词 独有结论1")]
    rows += [_row(i, f"通用 泛词 泛化记录{i}") for i in range(2, 9)]
    monkeypatch.setattr(brain, "extract_prompt_terms", lambda *a, **k: list(TERMS))
    monkeypatch.setattr(brain, "search_memories",
                        lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(brain, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_recall_project_block", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_memory_counters", lambda *a, **k: {})
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: list(sem))


def _sem(i, sim):
    return (i, 0.01, {"id": i, "_semantic_sim": sim})


def _recall(hub_root, tmp_path, origin="live", session="s1"):
    import brain
    return brain.prompt_recall(
        str(hub_root), PROMPT, agent="workbuddy", session=session,
        state_path=str(tmp_path / "state.json"),
        update_state=False, origin=origin)


def test_default_config_is_the_ab_winner_with_rails_intact():
    """默认必须是 A/B 胜出档，且四道护栏一条都不许放宽。

    0.60–0.62 是实测平台段（0.64 收益减半）；写成区间而不是钉死 0.62，
    是允许后续在平台内微调，但不允许「顺手」调到 0.40 或把 MAX 放开。
    """
    import brain
    assert brain.RECALL_SEM_RESCUE_MODE == "replace", brain.RECALL_SEM_RESCUE_MODE
    assert 0.60 <= brain.RECALL_SEM_RESCUE_SIM <= 0.62
    assert brain.RECALL_SEM_RESCUE_TOPN <= 3
    assert brain.RECALL_SEM_RESCUE_MAX == 1
    assert brain.RECALL_SEM_RESCUE_MARGIN >= 0.10
    # 语义重排权重不许因为「救援上线」被放大（R899 只在 12 条上标定过 10.0）
    assert 0 < brain.RECALL_SEM_RERANK_W <= 10.0


def test_rescue_default_actually_fires(hub_root, tmp_path, monkeypatch):
    """默认档不是摆设：真触发一次救援，且注入里出现被救回的候选。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, [_sem(5, 0.95), _sem(1, 0.20)])
    out = _recall(hub_root, tmp_path)
    assert out["sem_rescued"] == 1, out
    assert 5 in out["memory_ids"], out["memory_ids"]


def test_rescue_writes_journal_with_origin_and_clean_backup(hub_root, tmp_path,
                                                            monkeypatch):
    """救援触发要留流水，note 带 origin，且诊断串不许落进 backup（记忆 #569）。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, [_sem(5, 0.95), _sem(1, 0.20)])
    _recall(hub_root, tmp_path, origin="live")
    rows = [r for r in brain.journal_list(str(hub_root), limit=50)
            if r.get("action") == "brain_recall_sem_rescue"]
    assert rows, "救援触发了却没写流水，线上无法回答『触发几次』"
    note = rows[0].get("note") or ""
    assert "origin=live" in note, note
    assert rows[0].get("backup") == "", f"诊断串又写进 backup 了：{rows[0]}"


def test_probe_origin_is_not_mislabelled_live(hub_root, tmp_path, monkeypatch):
    """量具/探针的触发必须标成 probe，不能冒充真实会话。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, [_sem(5, 0.95), _sem(1, 0.20)])
    _recall(hub_root, tmp_path, origin="probe", session="probe-s")
    rows = [r for r in brain.journal_list(str(hub_root), limit=50)
            if r.get("action") == "brain_recall_sem_rescue"]
    assert rows and "origin=probe" in (rows[0].get("note") or ""), rows


def test_no_rescue_writes_no_journal(hub_root, tmp_path, monkeypatch):
    """没救援就不写流水——别把账本灌成噪声。"""
    import brain
    brain.init_db(str(hub_root))
    _pool(monkeypatch, [])   # 语义路没预热 → fail-open，零救援
    out = _recall(hub_root, tmp_path)
    assert out["sem_rescued"] == 0, out
    rows = [r for r in brain.journal_list(str(hub_root), limit=50)
            if r.get("action") == "brain_recall_sem_rescue"]
    assert not rows, rows


def test_origin_report_refuses_to_conclude_without_live(monkeypatch):
    """live=0 时报表必须明说『不下结论』，不许拿 probe/eval 当线上收益。"""
    import brain
    import rescue_origin_report_r901 as rep
    fake = [
        {"ts": "2026-11-06T09:00:00", "action": "brain_recall_sem_rescue",
         "note": "n=1 origin=eval"},
        {"ts": "2026-11-06T09:01:00", "action": "brain_recall_semantic_bridge",
         "note": "n=1 origin=probe"},
    ]
    monkeypatch.setattr(brain, "journal_list", lambda *a, **k: fake)
    data = rep.collect("x", days=30)
    assert data["counts"]["brain_recall_sem_rescue"]["eval"] == 1
    assert data["counts"]["brain_recall_semantic_bridge"]["probe"] == 1
    assert "不下结论" in rep.render(data), rep.render(data)


def test_origin_report_counts_live_separately(monkeypatch):
    """有 live 样本时如实累计，且仍把 eval 单列。"""
    import brain
    import rescue_origin_report_r901 as rep
    fake = [
        {"ts": "2026-11-06T09:00:00", "action": "brain_recall_sem_rescue",
         "note": "n=1 origin=live"},
        {"ts": "2026-11-06T09:01:00", "action": "brain_recall_sem_rescue",
         "note": "n=1 origin=live"},
        {"ts": "2026-11-06T09:02:00", "action": "brain_recall_sem_rescue",
         "note": "n=1 origin=eval"},
    ]
    monkeypatch.setattr(brain, "journal_list", lambda *a, **k: fake)
    text = rep.render(rep.collect("x", days=30))
    assert "累计触发 2 次" in text, text
