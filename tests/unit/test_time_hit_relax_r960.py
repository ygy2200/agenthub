# -*- coding: utf-8 -*-
"""R960（B118）：时间窗内候选的命中门槛放宽——先在旧码上红。

R955/R956 只读取证：开窗后「上周的踩坑经验有哪些」这类宽泛列举型提问，
窗内确有 15 条候选，却全被 hit 闸门删光（need=2，只命中 1 个词就出局）。
时间窗已经把窗外噪声挡在门外，窗内再把门槛守到 2 就是双重过滤。

本轮旋钮 `RECALL_TIME_HIT_RELAX`（默认 0=off）：
  · 只在「开窗 + 确认日期落在窗内」的候选上把 need 降到 1；
  · 取不到日期的 fail-open 候选**不打标、不放宽**（否则无日期噪声一起灌进来）；
  · 不动任何打分权重，不动 idf / rare_line（R946 实锤动评分代价 19.4pp）；
  · 无时间词的提问开关前后逐位相同。
旧码上常量、诊断字段、打标都不存在 → 全红。

夹具沿用 R953 教训（记忆 #597）：补 37 条不含提问词的填充行，避免 3 条同内容
候选在小池 rare_line(3)=0.98 下结构性失明——那是测试自身范围错，不是生产缺陷。
"""
import datetime as dt


def _recall(hub_root, prompt, tmp_path, session="s1"):
    import brain
    return brain.prompt_recall(
        str(hub_root), prompt, agent="workbuddy",
        session=session, state_path=str(tmp_path / "state.json"),
        update_state=False, origin="eval")


def _patch(monkeypatch, mems, recs=None, dates=None):
    import brain
    monkeypatch.setattr(brain, "search_memories",
                        lambda *a, **k: [dict(r) for r in mems])
    monkeypatch.setattr(brain, "search_records",
                        lambda *a, **k: [dict(r) for r in (recs or [])])
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: [])
    if dates is not None:
        monkeypatch.setattr(brain, "_memory_dates", lambda *a, **k: dict(dates))


def _mem(i, text):
    return {"id": i, "content": text, "tags": "", "kind": "lesson"}


def _filler(n=37):
    return [_mem(100 + i, "无关填充内容") for i in range(n)]


def test_default_off_and_diagnostics_exist(hub_root, tmp_path, monkeypatch):
    import brain
    brain.init_db(str(hub_root))
    assert brain.RECALL_TIME_HIT_RELAX == 0
    _patch(monkeypatch, [_mem(1, "门禁结果怎么查")] + _filler(), dates={})
    monkeypatch.setattr(brain, "RECALL_TIME_WINDOW", 1)
    out = _recall(hub_root, "今天的门禁结果", tmp_path)
    assert out.get("time_hit_relaxed") == 0, out


def test_relax_admits_in_window_single_hit_only(hub_root, tmp_path, monkeypatch):
    """窗内只命中 1 个词的候选被放宽；窗外、无日期、原门槛候选行为不变。

    R960 夹具修正：初版混了一条「命中 4 个词」的强候选，放宽后单命中候选
    虽进了打分池，仍被 RELATIVE margin（0.62）挤掉（探针实测 dropped.cap=1）——
    那是排序选择器的既定行为，本轮不动（R946 实锤动排序代价）。真实 B118
    场景是「窗内候选全被 hit 卡死、没有强候选压着」，故夹具不掺强候选。
    """
    import brain
    brain.init_db(str(hub_root))
    today = dt.date.today().isoformat()
    old = (dt.date.today() - dt.timedelta(days=20)).isoformat()
    mems = ([_mem(1, "门禁"),                    # 窗内，只命中 1 个词（B118 主角）
             _mem(2, "无关窗内内容"),            # 窗内，0 命中（守卫，不进打分池）
             _mem(3, "门禁结果旧记录"),          # 窗外，被时间窗删
             _mem(4, "门禁")]                   # 无日期 fail-open，不放宽
            + _filler())
    _patch(monkeypatch, mems, dates={1: today, 2: today, 3: old})
    monkeypatch.setattr(brain, "RECALL_TIME_WINDOW", 1)

    monkeypatch.setattr(brain, "RECALL_TIME_HIT_RELAX", 0)
    base = _recall(hub_root, "今天的门禁结果", tmp_path, session="base")
    assert 1 not in base["memory_ids"], base["memory_ids"]

    monkeypatch.setattr(brain, "RECALL_TIME_HIT_RELAX", 1)
    on = _recall(hub_root, "今天的门禁结果", tmp_path, session="on")
    assert 1 in on["memory_ids"], "窗内单命中候选应被放宽：" + str(on["memory_ids"])
    assert 2 not in on["memory_ids"], "0 命中候选不许被放宽放进来"
    assert 3 not in on["memory_ids"], "窗外候选仍须被时间窗删"
    assert 4 not in on["memory_ids"], "无日期候选不许跟着放宽灌噪声"
    assert on["time_hit_relaxed"] >= 1, on
    assert on["dropped"]["hit"] < base["dropped"]["hit"], (base["dropped"], on["dropped"])


def test_no_time_word_keeps_behavior_identical(hub_root, tmp_path, monkeypatch):
    import brain
    brain.init_db(str(hub_root))
    _patch(monkeypatch, [_mem(1, "门禁"), _mem(2, "门禁结果怎么查")] + _filler(),
           dates={1: "2026-11-01", 2: "2026-11-06"})
    monkeypatch.setattr(brain, "RECALL_TIME_WINDOW", 1)
    monkeypatch.setattr(brain, "RECALL_TIME_HIT_RELAX", 0)
    base = _recall(hub_root, "门禁结果怎么查", tmp_path, session="base")
    monkeypatch.setattr(brain, "RECALL_TIME_HIT_RELAX", 1)
    on = _recall(hub_root, "门禁结果怎么查", tmp_path, session="on")
    assert on["time_window"] == [], on
    assert on["time_hit_relaxed"] == 0, on
    assert on["memory_ids"] == base["memory_ids"], (base["memory_ids"], on["memory_ids"])


def test_record_route_also_relaxes(hub_root, tmp_path, monkeypatch):
    import brain
    brain.init_db(str(hub_root))
    today = dt.date.today().isoformat()
    recs = [{"id": 7, "title": "门禁", "content": "当天记录", "date": today}]
    _patch(monkeypatch, [], recs=recs, dates={})
    monkeypatch.setattr(brain, "RECALL_TIME_WINDOW", 1)
    monkeypatch.setattr(brain, "RECALL_TIME_HIT_RELAX", 0)
    base = _recall(hub_root, "今天的门禁结果", tmp_path, session="base")
    monkeypatch.setattr(brain, "RECALL_TIME_HIT_RELAX", 1)
    on = _recall(hub_root, "今天的门禁结果", tmp_path, session="on")
    assert on["time_hit_relaxed"] >= 1, on
    assert 7 in on["record_ids"], on["record_ids"]


def test_relax_does_not_reorder_existing_candidates(hub_root, tmp_path, monkeypatch):
    """只放宽准入、不重排：两档都能进的候选，相对顺序逐位不变。"""
    import brain
    brain.init_db(str(hub_root))
    today = dt.date.today().isoformat()
    mems = [_mem(1, "门禁结果 甲"), _mem(2, "门禁结果 乙")] + _filler()
    _patch(monkeypatch, mems, dates={1: today, 2: today})
    monkeypatch.setattr(brain, "RECALL_TIME_WINDOW", 1)
    monkeypatch.setattr(brain, "RECALL_TIME_HIT_RELAX", 0)
    base = _recall(hub_root, "今天的门禁结果甲", tmp_path, session="base")
    monkeypatch.setattr(brain, "RECALL_TIME_HIT_RELAX", 1)
    on = _recall(hub_root, "今天的门禁结果甲", tmp_path, session="on")
    ids_base = [x for x in base["memory_ids"] if x in (1, 2)]
    ids_on = [x for x in on["memory_ids"] if x in (1, 2)]
    assert ids_on == ids_base, (ids_base, ids_on)
