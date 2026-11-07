# -*- coding: utf-8 -*-
"""R953（B116）：时间限窗「只约束候选、不重排」——先在旧码上红。

R948 实锤：8 组时间词对照里 6/8 注入逐条相同、4/5 注入记忆越窗。
R946 又证明动评分/闸门会把代价外溢到 Recall@3（-19.4pp），所以本轮
**不碰任何打分权重**，只在打分前把候选池里日期落在窗口外的行剔掉。

与既有实现的区别：记录检索有 `_time_boost`（窗内加分，属重排）；
这里是记忆召回路径第一次接时间，且是过滤不是加分。日期口径复用
R913 的 `_memory_dates`（updated 优先、created 兜底、按 id 回查），
取不到日期一律 fail-open 保留——宁可少滤也不误杀。

判据口径：默认 0=off；开窗时 `time_window` / `time_window_dropped` 必须可观测；
无时间词的提问开关前后逐位相同。旧码上这些字段和常量都不存在 → 全红。
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


def _mem(i, text="门禁结果怎么查"):
    return {"id": i, "content": text, "tags": "", "kind": "lesson"}


def test_default_is_off_and_diagnostics_exist(hub_root, tmp_path, monkeypatch):
    import brain
    brain.init_db(str(hub_root))
    assert brain.RECALL_TIME_WINDOW == 0
    _patch(monkeypatch, [_mem(1)])
    out = _recall(hub_root, "昨天的门禁结果", tmp_path)
    assert out.get("time_window") == [], out
    assert out.get("time_window_dropped") == 0, out
    assert 1 in out["memory_ids"], out["memory_ids"]


def test_enabled_window_drops_out_of_range_memories(hub_root, tmp_path, monkeypatch):
    """窗口 = 昨天；窗内 1、窗外 2、缺日期 3（fail-open 保留）。

    R953 接手时修正夹具：原版只有 3 条内容完全相同、且全含提问词的候选，
    `_recall_idf` 在这个池里把每个提问词都算成泛词（df=3/3），rare_line(3)=0.98
    一条都够不着 → strong 空、注入空。**默认关档同样注入为空**，所以这是用例
    自身范围写错（记忆 #505 / #547 的小池结构性失明），不是生产缺陷。补 37 条
    不含提问词的填充行，让 idf 口径贴近真库（N=40、df=3 → raw idf≈2.46>1.90），
    这样验的才是「时间窗剔了谁」，不掺稀有词闸门的噪声。
    """
    import brain
    brain.init_db(str(hub_root))
    today = dt.date.today()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    old = (today - dt.timedelta(days=20)).isoformat()
    filler = [{"id": 100 + i, "content": "无关填充内容", "tags": "", "kind": "lesson"}
              for i in range(37)]
    _patch(monkeypatch, [_mem(1), _mem(2), _mem(3)] + filler,
           dates={1: yesterday, 2: old})
    monkeypatch.setattr(brain, "RECALL_TIME_WINDOW", 1)
    out = _recall(hub_root, "昨天的门禁结果", tmp_path)
    assert out["time_window"] == [yesterday, yesterday], out
    assert 1 in out["memory_ids"], out["memory_ids"]
    assert 2 not in out["memory_ids"], "窗外候选不许进注入：" + str(out["memory_ids"])
    assert 3 in out["memory_ids"], "取不到日期必须 fail-open 保留"
    assert out["time_window_dropped"] == 1, out


def test_no_time_word_keeps_behavior_identical(hub_root, tmp_path, monkeypatch):
    """无时间词 → 开关前后逐位一致（不许为一半的提问偷偷改行为）。"""
    import brain
    brain.init_db(str(hub_root))
    _patch(monkeypatch, [_mem(1), _mem(2)],
           dates={1: "2026-11-01", 2: "2026-11-06"})
    base = _recall(hub_root, "门禁结果怎么查", tmp_path, session="base")
    monkeypatch.setattr(brain, "RECALL_TIME_WINDOW", 1)
    on = _recall(hub_root, "门禁结果怎么查", tmp_path, session="on")
    assert on["time_window"] == [], on
    assert on["time_window_dropped"] == 0, on
    assert on["memory_ids"] == base["memory_ids"], (base["memory_ids"], on["memory_ids"])


def test_record_route_is_also_constrained(hub_root, tmp_path, monkeypatch):
    """记录路同样受窗口约束，且计数合并到同一个 dropped 字段。"""
    import brain
    brain.init_db(str(hub_root))
    today = dt.date.today()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    old = (today - dt.timedelta(days=20)).isoformat()
    recs = [{"id": 7, "title": "门禁结果", "content": "昨天的记录", "date": yesterday},
            {"id": 8, "title": "门禁结果", "content": "更早的记录", "date": old}]
    _patch(monkeypatch, [], recs=recs, dates={})
    monkeypatch.setattr(brain, "RECALL_TIME_WINDOW", 1)
    out = _recall(hub_root, "昨天的门禁结果", tmp_path)
    assert 8 not in out["record_ids"], out["record_ids"]
    assert out["time_window_dropped"] >= 1, out


def test_time_window_does_not_change_scoring_weights(hub_root, tmp_path, monkeypatch):
    """「只约束不重排」的守卫：同一批窗内候选，开窗前后相对顺序不变。"""
    import brain
    brain.init_db(str(hub_root))
    today = dt.date.today().isoformat()
    _patch(monkeypatch, [_mem(1, "门禁结果 甲"), _mem(2, "门禁结果 乙")],
           dates={1: today, 2: today})
    base = _recall(hub_root, "今天的门禁结果甲", tmp_path, session="base")
    monkeypatch.setattr(brain, "RECALL_TIME_WINDOW", 1)
    on = _recall(hub_root, "今天的门禁结果甲", tmp_path, session="on")
    assert on["memory_ids"] == base["memory_ids"], (base["memory_ids"], on["memory_ids"])
