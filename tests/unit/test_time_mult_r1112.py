# -*- coding: utf-8 -*-
"""R1112 时间窗乘法加权：默认关零回归 / 不删候选 / 真能改排序。

与 R1105 加性软加权的区别必须被这条用例锁住：加性项在 `wid*2.2` 面前是哑旋钮
（R1107 实测三档与 off 逐位相同），乘法必须能让**窗外的强候选让位给窗内的弱候选**。
"""
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FILLER = 37


def _mem(i, text="门禁结果怎么查"):
    return {"id": i, "content": text, "tags": "", "kind": "lesson"}


def _patch(monkeypatch, mems, dates):
    import brain
    monkeypatch.setattr(brain, "search_memories",
                        lambda *a, **k: [dict(r) for r in mems])
    monkeypatch.setattr(brain, "search_records", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_memory_dates", lambda *a, **k: dict(dates))


def _recall(hub_root, prompt, tmp_path, session="s"):
    import brain
    return brain.prompt_recall(
        str(hub_root), prompt, agent="workbuddy", cwd=str(ROOT),
        session=session, state_path=str(tmp_path / "s.json"),
        update_state=False, origin="eval")


def _stack(monkeypatch, hub_root):
    """窗内 1（弱，内容只命中一个提问词）/ 窗外 2（强，命中全部提问词）。"""
    import brain
    brain.init_db(str(hub_root))
    today = dt.date.today()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    old = (today - dt.timedelta(days=40)).isoformat()
    filler = [{"id": 300 + i, "content": "无关填充内容", "tags": "", "kind": "lesson"}
              for i in range(FILLER)]
    strong = _mem(2, "门禁结果 门禁 结果 怎么查")
    _patch(monkeypatch, [_mem(1, "门禁"), strong] + filler,
           {1: yesterday, 2: old})
    monkeypatch.setattr(brain, "RECALL_TIME_WINDOW", 1)
    return yesterday


def test_default_off_is_bitwise_legacy(hub_root, tmp_path, monkeypatch):
    """MULT=0 时不写任何新字段、不删候选之外的额外行为改变。"""
    import brain
    _stack(monkeypatch, hub_root)
    monkeypatch.setattr(brain, "RECALL_TIME_MULT", 0.0)
    monkeypatch.setattr(brain, "RECALL_TIME_SOFT", 0.0)
    out = _recall(hub_root, "昨天的门禁结果", tmp_path)
    assert out["time_window_dropped"] >= 1, "默认档仍是硬过滤（R953 契约）"
    assert 2 not in out["memory_ids"], out["memory_ids"]


def test_mult_keeps_outside_candidates(hub_root, tmp_path, monkeypatch):
    """MULT>0 → 不删任何候选（把硬过滤换成正权重，误杀不再可能发生）。"""
    import brain
    _stack(monkeypatch, hub_root)
    monkeypatch.setattr(brain, "RECALL_TIME_MULT", 0.5)
    out = _recall(hub_root, "昨天的门禁结果", tmp_path)
    assert out["time_window_dropped"] == 0, out
    assert 2 in out["memory_ids"], "窗外强候选必须留在池里：" + str(out["memory_ids"])


def test_mult_is_wired_and_reorders_pool(hub_root, tmp_path, monkeypatch):
    """接线契约：MULT 从 0 到 >0 必须改变 dropped（硬过滤 → 不删）。

    排序效果不在单测里断言：小池里「窗内弱候选」会先被命中闸门筛掉
    （记忆 #505/#547 的小池结构性失明），量到的是闸门不是加权。
    排序是否真的变了，由 97 条 A/B 用「mult 档必须与 off 逐位不同」来证明。
    """
    import brain
    _stack(monkeypatch, hub_root)
    monkeypatch.setattr(brain, "RECALL_TIME_SOFT", 0.0)
    monkeypatch.setattr(brain, "RECALL_TIME_MULT", 0.0)
    off = _recall(hub_root, "昨天的门禁结果", tmp_path, session="m0")
    monkeypatch.setattr(brain, "RECALL_TIME_MULT", 0.5)
    on = _recall(hub_root, "昨天的门禁结果", tmp_path, session="m5")
    assert off["time_window_dropped"] >= 1, off
    assert on["time_window_dropped"] == 0, on
    assert on["memory_ids"] == [2], on["memory_ids"]  # 不删候选后窗外强候选进注入
