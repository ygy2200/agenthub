# -*- coding: utf-8 -*-
"""R861：语义救援准入（B035）。

病根：`prompt_recall` 的词法两路有自己的门槛（命中数 + 稀有词 IDF + 相对 margin）。
词法全弱时它直接闭嘴——而「问法不同、库里没有那些字」恰恰是语义该补的洞。
救援的定位很窄：**只在词法一条候选都没有时**才启用，且候选要过比融合路更严的
0.62 线（绕过词法闸门直接进上下文，错了就是一条无依据的注入）。有候选但没挑出强的
属于 R825/R826 的 nudge 领地——那里语义不许插话（门禁把第一版 `not picked` 顶红过）。

本文件守：触发条件、阈值、上限、免疫/去重过滤、关闭开关、流水标记。
真实权重的语义质量由 tools/recall_eval.py 与 live 探针覆盖。
"""
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import brain  # noqa: E402


def _row(mid, sim, inject=0, adopt=0, last=""):
    return {"id": mid, "content": f"记忆 {mid}", "kind": "lesson",
            "project": "", "tags": "", "pinned": 0,
            "inject_count": inject, "adopt_count": adopt, "last_inject": last,
            "use_count": 0, "_semantic_sim": sim}


def _rank_returns(rows):
    return lambda root, query, entity, limit=100: [
        (r["id"], 1.0 / (61 + i), r) for i, r in enumerate(rows)]


def test_低于阈值不收_达到阈值才收(monkeypatch):
    monkeypatch.setattr(brain, "_RECALL_SEM_MIN", 0.62)
    monkeypatch.setattr(brain, "_semantic_rank", _rank_returns([
        _row(1, 0.6199), _row(2, 0.62), _row(3, 0.70)]))
    monkeypatch.setattr(brain, "_memory_counters", lambda root, ids: {})
    out = brain._semantic_rescue("root", "任何一句有中文的话", set())
    assert [r["id"] for r in out] == [2, 3]
    assert out[0]["_semantic_bridge"] == 0.62
    assert out[0]["_recall_hit"] == 0


def test_上限是三条与主通道配额一致(monkeypatch):
    monkeypatch.setattr(brain, "_RECALL_SEM_MIN", 0.62)
    monkeypatch.setattr(brain, "_semantic_rank", _rank_returns([
        _row(i, 0.9 - i * 0.01) for i in range(1, 7)]))
    monkeypatch.setattr(brain, "_memory_counters", lambda root, ids: {})
    out = brain._semantic_rescue("root", "一句中文", set())
    assert len(out) == brain._RECALL_SEM_CAP == 3


def test_本会话已注入的不重复出现(monkeypatch):
    monkeypatch.setattr(brain, "_RECALL_SEM_MIN", 0.62)
    monkeypatch.setattr(brain, "_semantic_rank", _rank_returns([_row(7, 0.8), _row(8, 0.79)]))
    monkeypatch.setattr(brain, "_memory_counters", lambda root, ids: {})
    out = brain._semantic_rescue("root", "一句中文", {7})
    assert [r["id"] for r in out] == [8]


def test_免疫条目不得借语义救援复活(monkeypatch):
    monkeypatch.setattr(brain, "_RECALL_SEM_MIN", 0.62)
    monkeypatch.setattr(brain, "_semantic_rank", _rank_returns([_row(9, 0.9), _row(10, 0.85)]))
    now = datetime.now().isoformat(timespec="seconds")
    monkeypatch.setattr(brain, "_memory_counters", lambda root, ids: {
        9: {"inject_count": 5, "adopt_count": 0, "last_inject": now},
        10: {"inject_count": 1, "adopt_count": 0, "last_inject": now},
    })
    out = brain._semantic_rescue("root", "一句中文", set())
    assert [r["id"] for r in out] == [10]


def test_开关关闭时一次都不查(monkeypatch):
    called = []
    monkeypatch.setattr(brain, "_RECALL_SEM_ENABLED", False)
    monkeypatch.setattr(brain, "_semantic_rank",
                        lambda *a, **k: called.append(1) or [])
    assert brain._semantic_rescue("root", "一句中文", set()) == []
    assert called == []


def test_词法全弱时才触发救援并标语义桥(hub_root, tmp_path, monkeypatch):
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "红色沙漠：删 DLSS Enabler 时只动游戏目录内的副本。",
                           kind="lesson", tags="红色沙漠,DLSS")
    called = {"n": 0}

    def _fake_rescue(root, prompt, excluded, limit=12):
        called["n"] += 1
        r = _row(mid, 0.71)
        r["_semantic_bridge"] = 0.71
        return [r]

    monkeypatch.setattr(brain, "_semantic_rescue", _fake_rescue)
    res = brain.prompt_recall(hub_root, "这个流程为什么要这样安排", agent="qoder",
                              session="", state_path=str(tmp_path / "s.json"),
                              update_state=False)
    assert called["n"] == 1
    assert res["memory_ids"] == [mid]
    assert res["semantic_bridge"] == 1
    assert "语义桥 0.71" in res["text"]


def test_已有词法强命中时不调用救援(hub_root, tmp_path, monkeypatch):
    brain.init_db(hub_root)
    mid = brain.add_memory(
        hub_root, "Windows 批处理：if 块内 echo 的括号必须写成 ^( ^)，否则 cmd 先解析整块。",
        kind="lesson", tags="失败分支,release.bat,退出码")
    called = []

    def _fake_rescue(root, prompt, excluded, limit=12):
        called.append(1)
        return []

    monkeypatch.setattr(brain, "_semantic_rescue", _fake_rescue)
    res = brain.prompt_recall(hub_root, "release.bat 失败分支的退出码怎么修", agent="qoder",
                              session="", state_path=str(tmp_path / "s.json"),
                              update_state=False)
    assert res["memory_ids"] and res["memory_ids"][0] == mid
    assert called == []
    assert res["semantic_bridge"] == 0


def test_泛化弱命中不启用救援_保留查库提示(hub_root, tmp_path, monkeypatch):
    """R825/R826 的边界：词法有候选、只是没挑出强的 → 只给 nudge，不许语义补摘要。

    2026-11-04 门禁实测：救援第一版用 `not picked` 当准入，把这条边界顶红了——
    6 条近义记忆的语义相似度 0.77–0.80，被当结论注入，正是 R826 要消灭的行为。
    """
    brain.init_db(hub_root)
    for i in range(6):
        brain.add_memory(hub_root, f"第{i}条：工作显示版本问题要按「显示」和「版本」"
                                   f"分开看，工作信息里的版本显示属于历史显示口径 {i}。",
                         kind="note")
    called = []

    def _fake_rescue(root, prompt, excluded, limit=12):
        called.append(1)
        return []

    monkeypatch.setattr(brain, "_semantic_rescue", _fake_rescue)
    res = brain.prompt_recall(hub_root, "这个工作显示版本是怎么回事呢", agent="qoder",
                              session="s2", state_path=str(tmp_path / "s.json"),
                              update_state=False)
    assert called == [], "有词法候选时不该走语义救援"
    assert res["memory_ids"] == []
    assert "库里没有能当结论的东西" in res["text"]
    assert "· #" not in res["text"]


def test_只有live注入才计曝光_eval与probe不得污染免疫账(hub_root, tmp_path):
    """量具反复跑不能把好记忆推成「推了 5 次零采纳」——那会让第二轮测的不是产品。"""
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "发布脚本失败分支的退出码排查：先看 cmd 括号转义。",
                           kind="lesson", tags="release.bat,失败分支,退出码")
    q = "release.bat 失败分支怎么修"

    def _injects():
        with brain.db_conn(hub_root) as c:
            return c.execute("SELECT inject_count FROM memories WHERE id=?", (mid,)).fetchone()[0]

    for origin in ("eval", "probe"):
        brain.prompt_recall(hub_root, q, agent="qoder", session=f"{origin}-1",
                            state_path=str(tmp_path / f"{origin}.json"),
                            update_state=False, origin=origin)
        brain.prompt_recall(hub_root, q, agent="qoder", session=f"{origin}-2",
                            state_path=str(tmp_path / f"{origin}.json"),
                            update_state=False, origin=origin)
        assert _injects() == 0, f"{origin} 不该计曝光"

    brain.prompt_recall(hub_root, q, agent="qoder", session="live-1",
                        state_path=str(tmp_path / "live.json"), update_state=False)
    assert _injects() == 1, "真实会话注入仍要计曝光"
