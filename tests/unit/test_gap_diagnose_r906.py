# -*- coding: utf-8 -*-
"""R906：零候选逐阶段归因工具的守卫用例。

钉的是「分类不能自说自话」三件事：
  ① 每个淘汰阶段都有独立用例（missing/not_in_pool/immune/hit/rare/injected）；
  ② 诊断必须与生产 `prompt_recall` 交叉校验——injected 的 id 真的在 memory_ids 里，
     伪造的生产结果要被判 inconsistent（记忆 #511：夹具不是生产形状等于没测）；
  ③ 工具是只读的：不写库、不写 state。

工具不存在时本文件 import 即红（新能力的红基线），其余用例在阶段判定写错时红。
"""
import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import brain  # noqa: E402
import gap_diagnose_r906 as gd  # noqa: E402


def _diag(hub_root, tmp_path, prompt, wants, **kw):
    return gd.diagnose_one(
        str(hub_root), prompt, wants,
        state_path=str(tmp_path / "state.json"), session="unit", **kw)


def _pool(monkeypatch, rows):
    """固定词法候选池，并关掉两条语义支路，让用例只测词法流水线。

    R906 首跑 2 红的根因就是没关桥：`prompt_recall` 在词法零候选时会走 R861
    语义救援把 id 注进 memory_ids，于是「not_in_pool」的生产结果反而是 injected。
    测试要测哪一层就固定哪一层，不能让可选能力偷偷改形状。
    """
    monkeypatch.setattr(brain, "search_memories",
                        lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(gd, "_sem_map", lambda *a, **k: {})
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_semantic_rescue", lambda *a, **k: [])


def _terms(prompt):
    return [t for t, _ in brain.extract_prompt_terms(prompt)]


def test_stage_missing_in_db(hub_root, tmp_path, monkeypatch):
    """答案 id 不在库里 → 先判标签过期，不许甩锅给召回。"""
    brain.init_db(str(hub_root))
    _pool(monkeypatch, [])
    rep = _diag(hub_root, tmp_path, "zetaquux 怎么修", [("mem", 999999)])
    a = rep["answers"][0]
    assert a["stage"] == "missing_in_db", a
    assert a["exists"] is False and a["in_final"] is False


def test_stage_not_in_pool(hub_root, tmp_path, monkeypatch):
    """答案在库里且 active，但没进 40 条候选池 → 抽词/粗筛问题，不是闸门。"""
    brain.init_db(str(hub_root))
    mid = brain.add_memory(str(hub_root), "zetaquux 的修法", kind="lesson")
    _pool(monkeypatch, [])
    rep = _diag(hub_root, tmp_path, "zetaquux 怎么修", [("mem", mid)])
    a = rep["answers"][0]
    assert a["exists"] is True, a
    assert a["stage"] == "not_in_pool", a
    assert rep["pool_size"] == 0


def test_stage_hit_gate(hub_root, tmp_path, monkeypatch):
    """进了池但命中词数 < need → hit_gate（与 prompt_recall 的 need 同口径）。"""
    brain.init_db(str(hub_root))
    mid = brain.add_memory(str(hub_root), "zetaquux 的修法", kind="lesson")
    _pool(monkeypatch, [{"id": mid, "content": "完全无关的另一件事", "tags": "",
                         "kind": "lesson", "status": "active"}])
    rep = _diag(hub_root, tmp_path, "zetaquux 怎么修", [("mem", mid)])
    a = rep["answers"][0]
    assert a["stage"] == "hit_gate", a
    assert a["hit"] < a["need"], a
    assert a["in_final"] is False


def test_stage_rare_gate(hub_root, tmp_path, monkeypatch):
    """过了 hit、命中词全泛 → rare_gate。这是 R906 真库三条 gap 的实际形态。"""
    brain.init_db(str(hub_root))
    prompt = "部署 校验 流程说明 怎么改"
    terms = _terms(prompt)
    assert len(terms) >= 2, terms
    common = " ".join(terms)
    ids = [brain.add_memory(str(hub_root), f"{common} 通用话术第{i}版",
                            kind="lesson", tags=",".join(terms))
           for i in range(6)]
    monkeypatch.setattr(gd, "_sem_map", lambda *a, **k: {})
    rep = _diag(hub_root, tmp_path, prompt, [("mem", ids[0])])
    a = rep["answers"][0]
    assert a["hit"] >= a["need"], a
    assert a["stage"] == "rare_gate", a
    assert a["best_raw"] < a["rare_line"], a


def test_stage_injected(hub_root, tmp_path, monkeypatch):
    """带独有稀有词的那条能进注入 → stage=injected 且 consistent。"""
    brain.init_db(str(hub_root))
    prompt = "部署 校验 zetaquux 怎么改"
    terms = _terms(prompt)
    common = " ".join(t for t in terms if t != "zetaquux")
    for i in range(6):
        brain.add_memory(str(hub_root), f"{common} 通用话术第{i}版",
                         kind="lesson", tags=",".join(terms))
    rare = brain.add_memory(str(hub_root), f"{common} 另有一个独有词 zetaquux 指这条",
                            kind="lesson", tags=",".join(terms))
    monkeypatch.setattr(gd, "_sem_map", lambda *a, **k: {})
    rep = _diag(hub_root, tmp_path, prompt, [("mem", rare)])
    a = rep["answers"][0]
    assert a["stage"] == "injected", a
    assert a["in_final"] is True and a["consistent"] is True
    assert rare in rep["final_ids"], rep["final_ids"]


def test_exposure_immune_is_its_own_stage(hub_root, tmp_path, monkeypatch):
    """曝光免疫要在 hit/rare 之前单列，否则会被误归因成闸门删的。"""
    import sqlite3
    brain.init_db(str(hub_root))
    mid = brain.add_memory(str(hub_root), "zetaquux 的修法", kind="lesson")
    prompt = "zetaquux 怎么修"
    _pool(monkeypatch, [{"id": mid, "content": "zetaquux 的修法", "tags": "",
                         "kind": "lesson", "status": "active"}])
    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    monkeypatch.setattr(brain, "_memory_counters",
                        lambda *a, **k: {mid: {"inject_count": 6, "adopt_count": 0,
                                               "last_inject": now, "use_count": 0}})
    rep = _diag(hub_root, tmp_path, prompt, [("mem", mid)])
    a = rep["answers"][0]
    assert a["immune"] is True, a
    assert a["stage"] == "exposure_immune", a
    assert a["in_final"] is False


def test_semantic_bridge_injection_is_consistent(hub_root, tmp_path, monkeypatch):
    """R861 语义桥注入池外近邻是合法路径，不许被误判成「诊断与生产不一致」。

    这是 R906 首跑 2 红暴露的工具缺陷：词法零候选时生产会走 `_semantic_rescue`
    注入一条根本不在词法池里的记忆，旧版诊断把它标成 inconsistent。修法认这条
    路径并单列 pipeline=semantic_bridge。
    """
    brain.init_db(str(hub_root))
    mid = brain.add_memory(str(hub_root), "zetaquux 的修法", kind="lesson")
    _pool(monkeypatch, [])  # 词法零候选
    monkeypatch.setattr(
        brain, "_semantic_rescue",
        lambda *a, **k: [{"id": mid, "content": "zetaquux 的修法", "tags": "",
                          "kind": "lesson", "_semantic_bridge": 0.8}])
    rep = _diag(hub_root, tmp_path, "zetaquux 怎么修", [("mem", mid)])
    a = rep["answers"][0]
    assert a["in_final"] is True, a
    assert a["consistent"] is True, a
    assert a["pipeline_stage"] == "semantic_bridge", a
    assert a["stage"] == "injected", a
    assert rep["inconsistent"] is False


def test_forged_production_output_flagged_inconsistent(hub_root, tmp_path, monkeypatch):
    """诊断不许自说自话：生产说没注入、流水线说全过 → 标 inconsistent。

    构造一个「过了闸门但生产 memory_ids 为空」的矛盾（例如有人改了 pick 逻辑却没
    同步诊断口径）。这时工具必须报警，而不是给出一条看似合理的 margin_cap 结论。
    """
    brain.init_db(str(hub_root))
    mid = brain.add_memory(str(hub_root), "zetaquux 的修法", kind="lesson")
    prompt = "zetaquux 怎么修"
    _pool(monkeypatch, [{"id": mid, "content": "zetaquux 的修法", "tags": "",
                         "kind": "lesson", "status": "active"}])
    # 让流水线判 passed_gates，但生产返回空 → 这里本应一致（margin_cap 是合法的），
    # 所以反过来伪造：流水线明确被闸门删，生产却说注入了。
    monkeypatch.setattr(brain, "_recall_hit_score", lambda *a, **k: 0)
    forged = {"memory_ids": [mid], "record_ids": [], "terms": []}
    rep = _diag(hub_root, tmp_path, prompt, [("mem", mid)], recall_out=forged)
    a = rep["answers"][0]
    assert a["in_final"] is True
    assert a["consistent"] is False, a
    assert a["pipeline_stage"] == "inconsistent", a
    assert rep["inconsistent"] is True


def test_diagnosis_is_read_only(hub_root, tmp_path, monkeypatch):
    """只读：诊断前后记忆条数不变，且不落 state 文件（update_state=False）。"""
    import sqlite3
    brain.init_db(str(hub_root))
    mid = brain.add_memory(str(hub_root), "zetaquux 的修法", kind="lesson")
    state = tmp_path / "state.json"
    conn = sqlite3.connect(str(brain.db_path(str(hub_root))))
    before = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
    conn.close()
    _pool(monkeypatch, [{"id": mid, "content": "zetaquux 的修法", "tags": "",
                         "kind": "lesson", "status": "active"}])
    _diag(hub_root, tmp_path, "zetaquux 怎么修", [("mem", mid)])
    conn = sqlite3.connect(str(brain.db_path(str(hub_root))))
    after = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
    conn.close()
    assert before == after
    assert not state.exists(), "诊断不许把伪会话写进曝光状态"
