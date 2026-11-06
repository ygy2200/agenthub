# -*- coding: utf-8 -*-
"""R905：给 brain_recall 流水补 origin/session，让「单会话连发」能被精确判定。

R902 用真库量化 B048 时只能按「单日 + agent」近似，因为 `brain_recall` 的
journal note 里既没有 session 也没有 origin——`--session` 参数其实早在钩子里传了
（brain_recall.mjs 第 64 行），只是写流水时没带上。R902 因此留下一条精度边界：
若日后要精确判「单会话连发」，需给流水补 session 字段（改写入侧，属另一轮）。
本轮就是那一轮，**不重开 B048 的结论**，只把观测粒度补齐。

修之前会怎样失败：
  ① note 里没有 origin=/session= → 前两条红；
  ② 按会话聚合的探针不存在 → AttributeError，后两条红。
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "tools"))


def _run(hub_root, tmp_path, monkeypatch, extra=()):
    import brain
    import brain_recall
    import core
    brain.init_db(str(hub_root))
    # 记忆 #492：独立脚本调 core.journal 前必须显式设 sink，否则流水只进旧
    # journal.jsonl；同时把 set_via 挡掉，避免改全局 via 污染同进程后续用例。
    monkeypatch.setattr(core, "JOURNAL_SINK", brain.journal_add)
    monkeypatch.setattr(brain, "set_via", lambda *a, **k: None)
    # 本文件测的是「main 怎么写流水」，不是召回本身。空临时库会让 prompt_recall
    # 返回空 text、main 直接早退不写流水（记忆 #511：夹具形状不是生产形状会被架空）。
    # 给一个固定 hint 形状：mem=[] 正是 R902 要按会话聚合的那一类。
    fake = {"text": "【AgentHub 即时回忆 · 测试用项目近况】", "memory_ids": [],
            "record_ids": [], "terms": ["麒麟"], "context_used": False}
    monkeypatch.setattr(brain, "prompt_recall", lambda *a, **k: dict(fake))
    argv = ["--root", str(hub_root), "--agent", "workbuddy",
            "--prompt", "麒麟 貔貅 白泽 会话标记测试",
            "--session", "s-905", "--origin", "probe",
            "--state", str(tmp_path / "recall.json"), "--no-state", "--json"]
    argv.extend(extra)
    brain_recall.main(argv)
    return [r for r in brain.journal_list(str(hub_root), limit=50)
            if r.get("action") == "brain_recall"]


def test_recall_journal_note_carries_origin_and_session(hub_root, tmp_path, monkeypatch):
    """流水 note 必须同时带 origin 与 session，且诊断串不许落进 backup。"""
    rows = _run(hub_root, tmp_path, monkeypatch)
    assert rows, "没写 brain_recall 流水"
    note = rows[0].get("note") or ""
    assert "origin=probe" in note, note
    assert "session=s-905" in note, note
    assert rows[0].get("backup") == "", f"诊断串又写进 backup 了：{rows[0]}"


def test_no_journal_flag_still_writes_nothing(hub_root, tmp_path, monkeypatch):
    """--no-journal 是测试用的逃生门，补字段不能把它弄坏。"""
    rows = _run(hub_root, tmp_path, monkeypatch, extra=("--no-journal",))
    assert not rows, rows


def test_session_probe_groups_hint_by_session(monkeypatch):
    """探针按会话聚合：单会话 hint 次数与最长连发都要算对。"""
    import brain
    import hint_session_probe_r905 as probe
    fake = [
        {"ts": f"2026-11-06T09:0{i}:00", "action": "brain_recall",
         "note": f"300 chars · mem=[] · ctx=0 · terms=x · origin=live · session=s-A"}
        for i in range(4)
    ] + [
        {"ts": "2026-11-06T09:10:00", "action": "brain_recall",
         "note": "883 chars · mem=[1,2] · ctx=0 · terms=x · origin=live · session=s-B"},
        {"ts": "2026-11-06T09:11:00", "action": "brain_recall",
         "note": "300 chars · mem=[] · ctx=0 · terms=x · origin=probe · session=s-B"},
    ]
    monkeypatch.setattr(brain, "journal_list", lambda *a, **k: fake)
    data = probe.collect("x", days=30)
    assert data["sessions"]["s-A"]["hint"] == 4, data["sessions"]
    assert data["sessions"]["s-A"]["streak"] == 4, data["sessions"]
    assert data["sessions"]["s-B"]["hint"] == 1, data["sessions"]
    assert data["sessions"]["s-B"]["streak"] == 1, data["sessions"]
    text = probe.render(data)
    assert "单会话 hint 峰值 4" in text, text
    assert "最长连发 4" in text, text


def test_session_probe_marks_legacy_rows_unknown(monkeypatch):
    """历史行没有 session 字段 → 单列 unknown，不冒充任何会话。"""
    import brain
    import hint_session_probe_r905 as probe
    fake = [
        {"ts": "2026-11-01T09:00:00", "action": "brain_recall",
         "note": "300 chars · mem=[] · ctx=0 · terms=x"},
        {"ts": "2026-11-06T09:00:00", "action": "brain_recall",
         "note": "300 chars · mem=[] · ctx=0 · terms=x · origin=live · session=s-A"},
    ]
    monkeypatch.setattr(brain, "journal_list", lambda *a, **k: fake)
    data = probe.collect("x", days=30)
    assert data["legacy"] == 1, data
    assert "s-A" in data["sessions"] and "unknown" not in data["sessions"], data
    assert "legacy（无 session）1 行" in probe.render(data)


def test_session_probe_refuses_to_conclude_without_session_rows(monkeypatch):
    """全是 legacy 行 = 结构性零样本，必须『不下结论』，不能拿零样本冒充 R902 结论。"""
    import brain
    import hint_session_probe_r905 as probe
    fake = [{"ts": "2026-11-01T09:00:00", "action": "brain_recall",
             "note": "300 chars · mem=[] · ctx=0 · terms=x"} for _ in range(3)]
    monkeypatch.setattr(brain, "journal_list", lambda *a, **k: fake)
    data = probe.collect("x", days=30)
    assert data["sessions"] == {} and data["legacy"] == 3
    text = probe.render(data)
    assert "不下结论" in text, text
    assert "与 R902" not in text, text
