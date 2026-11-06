# -*- coding: utf-8 -*-
"""R916：取池深度常量的守卫用例。

R915 探针量出 4 个期望答案被 limit=40 纯截断。本轮把硬编码提成常量供 A/B
切档，**默认仍为 40**（A/B 未证明收益前不许改生产行为）。旧码没有该常量、
且 search_memories 写死 40，本文件在旧码上必红。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import brain  # noqa: E402
import pool_limit_ab_r916 as ab  # noqa: E402


def test_constant_exists_and_default_is_unchanged():
    """默认必须还是 40：A/B 只是切档工具，不是偷偷改生产行为。"""
    assert hasattr(brain, "RECALL_POOL_LIMIT")
    assert brain.RECALL_POOL_LIMIT == 40, brain.RECALL_POOL_LIMIT


def _recall(hub_root, tmp_path, monkeypatch, seen, rows=None):
    brain.init_db(str(hub_root))

    def fake(root, query, **kw):
        seen.append(kw["limit"])
        return [dict(r) for r in (rows or [{"id": 1, "content": "zetaquux 的用法",
                                           "tags": "zetaquux", "kind": "lesson"}])]

    monkeypatch.setattr(brain, "search_memories", fake)
    monkeypatch.setattr(brain, "_semantic_rank", lambda *a, **k: [])
    monkeypatch.setattr(brain, "_semantic_rescue", lambda *a, **k: [])
    return brain.prompt_recall(
        str(hub_root), "zetaquux 怎么用", agent="workbuddy", session="unit",
        state_path=str(tmp_path / "state.json"), update_state=False, origin="eval")


def test_prompt_recall_passes_the_constant_to_search(hub_root, tmp_path, monkeypatch):
    seen = []
    _recall(hub_root, tmp_path, monkeypatch, seen)
    assert seen and seen[0] == brain.RECALL_POOL_LIMIT, seen


def test_changing_constant_changes_the_pool(hub_root, tmp_path, monkeypatch):
    """A/B 切常量必须真的作用到生产取池，不是离屏模拟。"""
    seen = []
    brain.RECALL_POOL_LIMIT = 100
    try:
        _recall(hub_root, tmp_path, monkeypatch, seen)
    finally:
        brain.RECALL_POOL_LIMIT = 40
    assert seen == [100], seen


def test_ab_tool_has_env_guard_and_restores_constant():
    """静态守卫：A/B 报表必须挂 R910 环境守卫，且 finally 恢复常量。"""
    src = (ROOT / "tools" / "pool_limit_ab_r916.py").read_text(encoding="utf-8")
    assert "env_guard_line" in src
    assert "brain.RECALL_POOL_LIMIT = saved" in src
    assert "finally:" in src


def test_ab_configs_cover_the_probe_platform():
    """档位要覆盖 R915 的平台段起点 100 与更大值，否则验不出平台。"""
    assert 40 in ab.DEFAULT_CONFIGS and 100 in ab.DEFAULT_CONFIGS
    assert max(ab.DEFAULT_CONFIGS) >= 200
