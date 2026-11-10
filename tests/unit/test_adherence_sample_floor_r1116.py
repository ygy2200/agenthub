# -*- coding: utf-8 -*-
"""R1116：采纳账定罪/报好的样本下限（judged < 50 不下结论）。

为什么：R852 把定罪分母换成「判过的」，但 `ADHERENCE_MIN_JUDGED=5` 只挡住了
「几乎没判过」。判过 8 笔、0 用上就下「注入的记忆真没被用上」这种硬结论，
统计上撑不住——8 笔的 95% 区间半宽远大于观察线本身，红一次就够让人去改注入策略。

外部依据（R1114/R1115 已检索，本轮直接引用）：
  · Applied Compute《Memory in the wild》：只统计 >15 次 retrieve 调用的 bucket
  · CSDN《agent记忆--记忆值不值：评测体系》：采纳率每题双跑、成本高，只在新版本上线前跑一次
  · lmversity《An Evaluation Harness》：「Statistical rigor in evals — how many items a claim needs」
  · bigyan.dev：小样本造出来的置信度比没有评测更危险（「manufactures confidence」）

钉三件事：小样本不判红也不报好 / 够样本仍照原判据定罪 / 下限写进 detail 可核查。
"""
import datetime

import brain
import ops

PROJECT = "采纳样本下限-测试-对象问题"


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _hub(root: str) -> int:
    brain.init_db(root)
    brain.add_record(root, PROJECT, "workbuddy", _now()[:10], "开工", "对齐进度")
    return brain.add_memory(root, "样本下限测试记忆：floor_r1116.py 的三态账。",
                            kind="lesson", tags="r1116", agent="qoder")


def _row(root: str, mid: int, used: int = 0, decided: bool = False) -> None:
    with brain.db_conn(root) as conn:
        conn.execute(
            "INSERT INTO recall_adherence(ts,session,agent,memory_id,project,strength,fp,"
            "used,decided_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (_now(), "s-floor", "qoder", mid, PROJECT, "strong",
             '["floor_r1116.py"]', used, _now() if decided else ""))


def _capture(root: str) -> None:
    """喂够口径的采集流水，确保能走到定罪那一层（否则红因会落在尺子上）。"""
    for _ in range(3):
        brain.journal_add(root, "qoder", "brain_adherence", "answer",
                          note="checked=4 matched=1 mem=[7] chars=4200 src=transcript sess=s-floor")


def test_small_sample_never_convicts_or_praises(hub_root):
    """判过 8 笔一笔没用上：口径诚实但样本太薄，不许定罪、也不许报好。"""
    mid = _hub(hub_root)
    for _ in range(8):
        _row(hub_root, mid, decided=True)
    _capture(hub_root)
    r = ops._adherence_health(hub_root)
    assert r["ok"] is True, r
    assert "真没被用上" not in r["summary"], r["summary"]
    assert "正常区间" not in r["summary"] and "偏低" not in r["summary"], r["summary"]
    assert "样本下限" in r["summary"] or "不判红" in r["summary"], r["summary"]


def test_large_sample_still_convicts(hub_root):
    """判过 60 笔 0 用上：够样本，原判据必须照常定罪（新门槛不能变成免死金牌）。"""
    mid = _hub(hub_root)
    for _ in range(60):
        _row(hub_root, mid, decided=True)
    _capture(hub_root)
    r = ops._adherence_health(hub_root)
    assert r["ok"] is False, r
    assert "真没被用上" in r["summary"], r["summary"]


def test_floor_is_declared_and_configurable(hub_root):
    mid = _hub(hub_root)
    for _ in range(8):
        _row(hub_root, mid, decided=True)
    _capture(hub_root)
    assert ops.ADHERENCE_MIN_CONFIDENT == 50
    r = ops._adherence_health(hub_root)
    assert any("50" in str(x) for x in r["detail"]), r["detail"]
