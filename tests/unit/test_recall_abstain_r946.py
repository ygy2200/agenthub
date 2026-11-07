# -*- coding: utf-8 -*-
"""R946（B114）：弃答地板——库里没答案时不许把 top-k 塞满。

立项证据（R945 真库基线）：12 条 abstention 样本、有效 9 条、假阳性 4 条（44.4%），
纯陌生组 4/4 全对、近形诱饵组 4/5 中招。根因不是「库空不空」，而是注入走
**相对 top-k**：只要候选池非空，过稀有词闸门的头几条就一定被注入；R826 的
「库里没有能当结论的东西」分支只在 `not picked and not rec_rows` 时才触发，
漏过「命中了一个外壳实体词、核心意图词在库里零命中」这整类。

外部依据：LongMemEval（ICLR 2025）abstention 能力；「总在回答的系统在 Recall@3 上
好看、在生产里是错的」。见 output/迭代依据-R944-...md A1/A2。

设计（默认关，切常量即可 A/B，与 R899/R901/R943 同款）：
  · `term_coverage` = 提问词里至少在**候选池正文**出现过的比例；
    池 = 记忆池 + 记录池（合并！否则「答案只在记录路」的样本会被误伤，
    recall_eval #11 就是 rec 路标签）。
  · `RECALL_ABSTAIN_MIN_COV`（默认 0=off）：coverage 低于线时清空 picked/rec_rows，
    复用 R826 已有的「别把下面当依据」文案，不发明第二套话术。
  · `abstained` / `abstain_dropped` 只读诊断，不动 `dropped` 五类计数（R898/R943 口径）。

本文件在旧码上全部必红（常量与字段都不存在）。四条覆盖：默认关、低覆盖真弃答、
高覆盖不弃答、**记录路答案不许被误伤**。
"""


def _recall(hub_root, prompt, tmp_path, session="s1"):
    import brain
    return brain.prompt_recall(
        str(hub_root), prompt, agent="workbuddy",
        session=session, state_path=str(tmp_path / "state.json"),
        update_state=False, origin="eval")


def _patch(monkeypatch, mems, recs=None):
    import brain
    monkeypatch.setattr(brain, "search_memories",
                        lambda *a, **k: [dict(r) for r in mems])
    monkeypatch.setattr(brain, "search_records",
                        lambda *a, **k: [dict(r) for r in (recs or [])])
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: [])


def test_default_is_off_and_diagnostics_exist(hub_root, tmp_path, monkeypatch):
    import brain
    brain.init_db(str(hub_root))
    assert brain.RECALL_ABSTAIN_MIN_COV == 0.0
    _patch(monkeypatch, [{"id": 1, "content": "zetaquux 的修法：先看配置",
                          "tags": "", "kind": "lesson"}])
    out = _recall(hub_root, "zetaquux 怎么修", tmp_path)
    assert isinstance(out.get("term_coverage"), float), out
    assert out.get("abstained") == 0, out
    assert out.get("abstain_dropped") == 0, out
    assert 1 in out["memory_ids"], out["memory_ids"]


def test_low_coverage_abstains_and_clears_both_routes(hub_root, tmp_path, monkeypatch):
    """核心意图词在池里零命中 → 记忆路与记录路都不许注入。"""
    import brain
    brain.init_db(str(hub_root))
    _patch(monkeypatch,
           [{"id": 1, "content": "CS2 卡死的排查记录", "tags": "", "kind": "lesson"}],
           [{"id": 9, "title": "CS2 卡死", "content": "排查过程", "date": "2026-11-01"}])
    base = _recall(hub_root, "CS2 的 quasarflux 服务端口是多少", tmp_path,
                   session="base")
    monkeypatch.setattr(brain, "RECALL_ABSTAIN_MIN_COV", 0.6)
    out = _recall(hub_root, "CS2 的 quasarflux 服务端口是多少", tmp_path,
                  session="on")
    assert out["abstained"] == 1, out
    assert out["memory_ids"] == [], out["memory_ids"]
    assert out["record_ids"] == [], out["record_ids"]
    # 清掉的条数必须等于基准档本来会注的条数——计数不撒谎（夹具里可能本来就是 0）。
    assert out["abstain_dropped"] == (len(base["memory_ids"]) +
                                      len(base["record_ids"])), (base, out)


def test_high_coverage_keeps_injection(hub_root, tmp_path, monkeypatch):
    """地板不许变成一刀切：覆盖率够时行为逐位不变。"""
    import brain
    brain.init_db(str(hub_root))
    _patch(monkeypatch, [{"id": 1, "content": "zetaquux 怎么修", "tags": "",
                          "kind": "lesson"}])
    base = _recall(hub_root, "zetaquux 怎么修", tmp_path, session="base")
    monkeypatch.setattr(brain, "RECALL_ABSTAIN_MIN_COV", 0.3)
    on = _recall(hub_root, "zetaquux 怎么修", tmp_path, session="on")
    assert on["abstained"] == 0, on
    assert on["memory_ids"] == base["memory_ids"], (base["memory_ids"], on["memory_ids"])


def test_record_only_answer_is_not_abstained(hub_root, tmp_path, monkeypatch):
    """反误伤：答案只在记录路时，coverage 必须由记录池撑起来（recall_eval #11）。"""
    import brain
    brain.init_db(str(hub_root))
    _patch(monkeypatch, [], [{"id": 9, "title": "工作台每组只显示二十条没有出口",
                              "content": "分页出口补上了", "date": "2026-11-01"}])
    monkeypatch.setattr(brain, "RECALL_ABSTAIN_MIN_COV", 0.5)
    out = _recall(hub_root, "工作台每组只显示二十条没有出口", tmp_path)
    assert out["term_coverage"] > 0, out
    assert out["abstained"] == 0, out
    assert 9 in out["record_ids"], out["record_ids"]
