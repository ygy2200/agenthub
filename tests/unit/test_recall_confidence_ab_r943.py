# -*- coding: utf-8 -*-
"""R943（B112）：confidence 分档的两个旋钮 + 量具纯函数。

为什么改：R939 量化出 confidence 591 条里 526 条恒 1.0（89%），全库唯一读取点是
记忆去重的 `_MEM_QUALS`，检索路径一次都没读过它——于是「按置信度分档」在生产上
等于不存在，而 next_candidates 要的是一句可验证的话：分档到底能不能改变任何一条
检索结论。要给这句话一个可复跑的答案，就得先把两种分档用法（降权 / 硬过滤）
做成**默认关闭、切常量即可生效**的旋钮。

本文件钉四件事（旧码上全部必红，因为两个常量和 dropped.conf 都不存在）：
  1. 默认档位是关的，且关闭时**一次库都不查**——钩子路径不许为实验付固定成本；
  2. 过滤档真会删候选，且只删低于线的、按 `dropped.conf` 计数（不静默）；
  3. 降权档的量级有界：权重 1 翻不动 6 分级的词法差，权重 20 翻得动；
  4. 量具的分档边界与 top3 差异函数是纯函数，错一档会让「低置信多少条」悄悄偏。
"""


def _recall(hub_root, prompt, tmp_path, session="s1"):
    import brain
    return brain.prompt_recall(
        str(hub_root), prompt, agent="workbuddy",
        session=session, state_path=str(tmp_path / "state.json"),
        update_state=False, origin="eval")


def test_defaults_are_off_and_query_is_skipped(hub_root, tmp_path, monkeypatch):
    """默认关 → 不查 confidence 表；这是「实验开关不许收生产过路费」的守卫。"""
    import brain
    brain.init_db(str(hub_root))
    assert brain.RECALL_CONF_W == 0.0, brain.RECALL_CONF_W
    assert brain.RECALL_CONF_MIN == 0.0, brain.RECALL_CONF_MIN

    def boom(*a, **k):
        raise AssertionError("默认档位不许查 confidence：钩子路径不接受为实验付固定成本")

    monkeypatch.setattr(brain, "_memory_confidence", boom)
    monkeypatch.setattr(
        brain, "search_memories",
        lambda *a, **k: [{"id": 1, "content": "zetaquux 的修法", "tags": "",
                          "kind": "lesson"}])
    out = _recall(hub_root, "zetaquux 怎么修", tmp_path)
    assert 1 in out["memory_ids"], out["memory_ids"]
    assert out["dropped"]["conf"] == 0, out["dropped"]


def test_min_filter_drops_low_confidence_and_counts(hub_root, tmp_path, monkeypatch):
    """过滤档：0.3 的那条不许进注入，且必须记在 dropped.conf 上（删了要留痕）。"""
    import brain
    brain.init_db(str(hub_root))
    pool = [
        {"id": 1, "content": "zetaquux 的修法：先看配置", "tags": "", "kind": "lesson"},
        {"id": 2, "content": "zetaquux 的旧说法，已被推翻", "tags": "", "kind": "lesson"},
        {"id": 3, "content": "zetaquux 的另一条说明", "tags": "", "kind": "lesson"},
    ]
    monkeypatch.setattr(brain, "search_memories", lambda *a, **k: [dict(r) for r in pool])
    monkeypatch.setattr(brain, "_memory_confidence",
                        lambda *a, **k: {1: 1.0, 2: 0.3, 3: 1.0})
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: [])
    monkeypatch.setattr(brain, "RECALL_CONF_MIN", 0.6)

    out = _recall(hub_root, "zetaquux 怎么修", tmp_path)
    assert 2 not in out["memory_ids"], out["memory_ids"]
    assert out["dropped"]["conf"] == 1, out["dropped"]
    assert 1 in out["memory_ids"], out["memory_ids"]


def test_boost_weight_only_flips_when_it_covers_the_gap(hub_root, tmp_path, monkeypatch):
    """降权档的量级边界：1 号词法更强但置信 0.2；W=1 翻不动，W=20 才翻得动。

    四条候选各带一枚独有词，保证都过得了稀有词闸门（只测置信度，不测闸门）；
    1 号多命中一个池内稀有词，词法差约 6 分——正是 R899 量到的「典型排序差」。
    """
    import brain
    brain.init_db(str(hub_root))
    pool = [
        {"id": 1, "content": "流程 校验 zetaquux 独有词甲 阿尔法", "tags": "", "kind": "lesson"},
        {"id": 2, "content": "流程 校验 zetaquux 甲版 贝塔", "tags": "", "kind": "lesson"},
        {"id": 3, "content": "流程 校验 zetaquux 乙版 伽马", "tags": "", "kind": "lesson"},
        {"id": 4, "content": "流程 校验 zetaquux 丙版 德尔塔", "tags": "", "kind": "lesson"},
    ]
    monkeypatch.setattr(brain, "search_memories", lambda *a, **k: [dict(r) for r in pool])
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_memory_confidence",
                        lambda *a, **k: {1: 0.2, 2: 1.0, 3: 1.0, 4: 1.0})
    prompt = "流程 校验 zetaquux 独有词甲 怎么修"

    base = _recall(hub_root, prompt, tmp_path, session="w0")
    assert base["memory_ids"][0] == 1, base["memory_ids"]

    monkeypatch.setattr(brain, "RECALL_CONF_W", 1.0)
    weak = _recall(hub_root, prompt, tmp_path, session="w1")
    assert weak["memory_ids"][0] == 1, weak["memory_ids"]

    monkeypatch.setattr(brain, "RECALL_CONF_W", 20.0)
    strong = _recall(hub_root, prompt, tmp_path, session="w20")
    # 20*(0.2-1.0) = -16 分：1 号不是「换个位次」，而是被 margin 直接挤出注入——
    # 这正是「无界降权」的真实形状，也是权重必须标定、不能拍脑袋给大数的理由。
    assert 1 not in strong["memory_ids"], strong["memory_ids"]


def _tool():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    if str(root / "tools") not in sys.path:
        sys.path.insert(0, str(root / "tools"))
    import confidence_ab_r943 as tool
    return tool


def test_conf_buckets_bounds():
    """分档边界：0.9 属于「0.9-1.0」，0.899 属于「0.8-0.9」；缺失值按 1.0（不猜低）。"""
    tool = _tool()
    b = tool.conf_buckets([1.0, 0.999, 0.9, 0.899, 0.8, 0.79, 0.6, 0.59, None])
    assert b["1.0"] == 2, b          # 1.0 与 None（缺失按满置信）
    assert b["0.9-1.0"] == 2, b      # 0.999 与 0.9
    assert b["0.8-0.9"] == 2, b      # 0.899 与 0.8
    assert b["0.6-0.8"] == 2, b      # 0.79 与 0.6
    assert b["<0.6"] == 1, b         # 0.59
    assert sum(b.values()) == 9, b


def test_diff_top3_ignores_order_and_reports_both_sides():
    tool = _tool()
    gained, lost = tool.diff_top3([1, 2, 3], [3, 2, 9])
    assert gained == [9], gained
    assert lost == [1], lost
    assert tool.diff_top3([1, 2, 3], [3, 2, 1]) == ([], [])
