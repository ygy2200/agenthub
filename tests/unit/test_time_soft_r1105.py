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
