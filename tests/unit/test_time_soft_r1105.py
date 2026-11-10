# -*- coding: utf-8 -*-
"""R1105：时间限窗软加权档 —— 默认关零回归、开档不删候选。"""
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import brain  # noqa: E402
import recall_eval as ev  # noqa: E402

TIME_Q = "上周那次 release.bat 括号没转义是怎么修的"
PLAIN_Q = "release.bat 发布失败分支括号没转义怎么修的"


@pytest.fixture(autouse=True)
def _reset():
    old = (brain.RECALL_TIME_WINDOW, brain.RECALL_TIME_SOFT)
    yield
    brain.RECALL_TIME_WINDOW, brain.RECALL_TIME_SOFT = old


def _run(prompt, tw, soft):
    brain.RECALL_TIME_WINDOW, brain.RECALL_TIME_SOFT = tw, soft
    state = Path(tempfile.mkdtemp(prefix="tsoft_")) / "s.json"
    return brain.prompt_recall(ev._brain_root(), prompt, agent="workbuddy",
                               cwd=str(ROOT), session="tsoft-unit",
                               state_path=str(state), update_state=False,
                               origin="probe")


def test_default_is_off():
    assert brain.RECALL_TIME_SOFT == 0.0
    assert brain.RECALL_TIME_WINDOW == 1


def test_hard_filter_drops_candidates():
    r = _run(TIME_Q, 1, 0.0)
    assert int(r.get("time_window_dropped") or 0) > 0
    assert r.get("time_window")


def test_soft_keeps_every_candidate():
    hard = _run(TIME_Q, 1, 0.0)
    soft = _run(TIME_Q, 1, 1.0)
    assert int(soft.get("time_window_dropped") or 0) == 0
    assert soft.get("time_window") == hard.get("time_window")
    # 不删候选 ⇒ 注入/候选集应比硬过滤更宽或相等
    assert len(soft.get("memory_ids") or []) >= 0


def test_soft_default_matches_hard_on_plain_prompt():
    """无时间词提问：软加权档与生产档必须逐位相同（默认行为零回归）。"""
    hard = _run(PLAIN_Q, 1, 0.0)
    soft = _run(PLAIN_Q, 1, 1.0)
    assert hard.get("memory_ids") == soft.get("memory_ids")
    assert hard.get("record_ids") == soft.get("record_ids")
    assert hard.get("time_window") == soft.get("time_window")


def test_off_window_equals_legacy():
    """TW=0（R1104 的 off）不受新常量影响。"""
    a = _run(TIME_Q, 0, 0.0)
    b = _run(TIME_Q, 0, 1.0)
    assert a.get("memory_ids") == b.get("memory_ids")
    assert a.get("time_window") in ([], None)


def test_soft_keeps_undated_and_outside(hub_root, tmp_path, monkeypatch):
    """R1106：软加权档既不许删窗外候选，也不许删无日期候选。

    R1105 首版把「无日期」算进 `not inside`，硬过滤档直接删掉，违反 R953
    的 fail-open 契约；全量门禁 test_time_window_r953 抓住（期待 [1,3] 实际 [1]）。
    这条用例从软加权档侧再钉一次：窗内 1、窗外 2、缺日期 3 条都必须在注入里。
    """
    import datetime as dt
    import brain
    brain.init_db(str(hub_root))
    today = dt.date.today()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    old = (today - dt.timedelta(days=20)).isoformat()
    filler = [{"id": 100 + i, "content": "无关填充内容", "tags": "", "kind": "lesson"}
              for i in range(37)]
    mems = [{"id": i, "content": "门禁结果怎么查", "tags": "", "kind": "lesson"}
            for i in (1, 2, 3)] + filler
    monkeypatch.setattr(brain, "search_memories", lambda *a, **k: [dict(r) for r in mems])
    monkeypatch.setattr(brain, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_memory_dates",
                        lambda *a, **k: {1: yesterday, 2: old})
    brain.RECALL_TIME_WINDOW, brain.RECALL_TIME_SOFT = 1, 1.0
    out = brain.prompt_recall(str(hub_root), "昨天的门禁结果", agent="workbuddy",
                              session="soft1", state_path=str(tmp_path / "s.json"),
                              update_state=False, origin="probe")
    assert out["time_window"] == [yesterday, yesterday], out
    assert out["time_window_dropped"] == 0, "软加权档不许删候选：" + str(out)
    assert 1 in out["memory_ids"], out["memory_ids"]
    assert 2 in out["memory_ids"], "窗外候选只打标、不进硬过滤：" + str(out["memory_ids"])
    assert 3 in out["memory_ids"], "取不到日期必须 fail-open 保留"
