# -*- coding: utf-8 -*-
"""R243：大脑页只为「屏幕上看得见的东西」取数。

定罪（真实库 best-of-3，output/_r243_brain_cost.txt）：
  一次同步刷新 173ms = health_report 167ms + 其余；
  167ms 里 similar_memories 125ms（75%）——算的是重复/矛盾对，
  大脑页从来不渲染这两块（它只读 memories/crystallization/recall_push_total/
  todo_count/top_pushed/search_by_agent/push_by_agent/todos）；
  「置顶记忆」为找 8 条付了 140ms：`search_memories("", "", 100)` 即使空查询
  也进融合检索（加载语义模型），再筛 pinned；
  「展开全部」多显示 5 行走 reload(force=True)，整次重读 428ms。

本文件钉四条不变量：
  · include_dups=False 真的不调 similar_memories，且把 dup 字段标成 None（不是 0）；
  · 瘦报告的「页面用得到的 8 个字段」与全量报告逐字相等（屏幕数据不许变）；
  · list_pinned_memories 与旧路径（search_memories 后筛 pinned）选出同一批 id；
  · 置顶直查不依赖检索通道（检索通道坏掉也照样出置顶）。
"""
import pytest


def _add(root, content, pinned=False, kind="fact", status="active", use_count=0):
    """直插记忆：绕开 add_memory 的嵌入钩子（unit 层不加载模型）。"""
    import brain
    with brain.db_conn(root) as conn:
        cur = conn.execute(
            "INSERT INTO memories(kind,content,project,agent,pinned,status,created,updated,"
            "use_count,maturity) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (kind, content, "P1", "qoder", 1 if pinned else 0, status,
             "2026-10-20T10:00:00", "2026-10-20T10:00:00", use_count, "draft"))
        return cur.lastrowid


@pytest.fixture
def hub(tmp_path):
    import brain
    root = str(tmp_path)
    brain.init_db(root)
    return root


def test_slim_report_skips_similar_memories(hub, monkeypatch):
    import brain
    calls = []
    real = brain.similar_memories

    def spy(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(brain, "similar_memories", spy)
    brain.health_report(hub, include_dups=True)
    assert len(calls) == 1, "默认路径必须保留重复分析（hub_health / 统计页在用）"
    brain.health_report(hub, include_dups=False)
    assert len(calls) == 1, "include_dups=False 仍在算 similar_memories——白付 125ms"


def test_slim_report_marks_dup_fields_as_not_computed(hub, monkeypatch):
    import brain
    monkeypatch.setattr(brain, "similar_memories", lambda *a, **kw: [{"a": 1, "b": 2}])
    slim = brain.health_report(hub, include_dups=False)
    assert slim["dup_memories"] is None
    assert slim["dup_memory_count"] is None, "没算过必须写 None，写 0 组会骗人"
    assert slim["conflict_pairs"] is None
    assert slim["conflict_pair_count"] is None
    full = brain.health_report(hub, include_dups=True)
    assert full["dup_memory_count"] == 1
    assert full["conflict_pair_count"] == 0


def test_slim_report_keeps_every_field_the_page_shows(hub, monkeypatch):
    """屏幕数据不许变：页面读的 8 个字段与全量报告逐字相等。"""
    import brain
    for i in range(6):
        _add(hub, f"记忆内容 {i}", pinned=(i < 3), use_count=i)
    old = brain.health_report(hub, include_dups=True)
    slim = brain.health_report(hub, include_dups=False)
    for key in ("memories", "crystallization", "recall_push_total", "todo_count",
                "top_pushed", "search_by_agent", "push_by_agent", "todos"):
        assert slim[key] == old[key], f"{key} 变了——瘦身只许省看不见的成本"


def test_pinned_list_matches_legacy_selection(hub):
    """新直查 vs 旧路径（search_memories 空查询后筛 pinned）选出同一批 id。"""
    import brain
    ids = [_add(hub, f"记忆 {i}", pinned=(i % 3 == 0)) for i in range(20)]
    legacy = [m["id"] for m in brain.search_memories(hub, "", "", 100) if m["pinned"]][:8]
    new = [m["id"] for m in brain.list_pinned_memories(hub, limit=8)]
    assert new == legacy, f"置顶清单变了：new={new} legacy={legacy}"
    assert all(i in ids for i in new)


def test_pinned_list_respects_limit_and_excludes_deleted(hub):
    import brain
    _add(hub, "要删的置顶", pinned=True, status="deleted")
    keep = _add(hub, "留着的置顶", pinned=True)
    loose = _add(hub, "普通记忆", pinned=False)
    rows = brain.list_pinned_memories(hub, limit=8)
    got = [r["id"] for r in rows]
    assert keep in got and loose not in got
    assert len(got) == 1
    assert set(rows[0]) >= {"id", "kind", "content", "use_count"}, "行字段不够画一行"


def test_pinned_list_does_not_go_through_search(hub, monkeypatch):
    """检索通道坏掉不该影响置顶——这正是 140ms 的来源。"""
    import brain

    def boom(*a, **kw):
        raise AssertionError("置顶不该走检索")

    monkeypatch.setattr(brain, "search_memories", boom)
    _add(hub, "置顶一条", pinned=True)
    assert len(brain.list_pinned_memories(hub, limit=8)) == 1
