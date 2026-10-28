# -*- coding: utf-8 -*-
r"""项目详情必须报「共几条」（R260）。

缺陷：`brain.get_project` 只 `LIMIT 5` 取最近记录，**不返回总数**，MCP 渲染也只写
「最近记录：」。真实项目有 359 条记录时，调用方看到 5 条会以为这就是全部——信息缺失
比信息过载更难发现（过载会被嫌啰嗦，缺失没人抱怨，只是悄悄判断错）。

守四件事：
  ① 记录多于 5 条时 `total` 是真实总数、`shown` 是实际列出条数；
  ② 记录少于上限时不出现「只列最近 N 条」的误导文案；
  ③ 空项目 total=0 且不崩；
  ④ `limit` 可调且仍受上限约束（不给调用方拉爆的机会）。
"""
import brain


def _seed(root, n):
    """项目由 add_record 自动登记（brain 没有单独的 add_project）。"""
    brain.init_db(root)
    for i in range(n):
        brain.add_record(root, "项目A", "t", f"2026-10-{i % 28 + 1:02d}", f"记录{i}", f"内容{i}")
    return "项目A"


def test_total_counts_all_active(hub_root):
    """① 20 条记录：total=20，shown=5。"""
    name = _seed(hub_root, 20)
    p = brain.get_project(hub_root, name)
    assert p["total"] == 20, p
    assert p["shown"] == 5 and len(p["records"]) == 5


def test_small_project_not_misleading(hub_root):
    """② 只有 3 条：total=3、shown=3（渲染层据此不再提示「还有更多」）。"""
    name = _seed(hub_root, 3)
    p = brain.get_project(hub_root, name)
    assert (p["total"], p["shown"]) == (3, 3), p


def test_empty_project(hub_root):
    """③ 空项目：total=0、records 为空，不抛。"""
    brain.init_db(hub_root)
    with brain.db_conn(hub_root) as c:          # 只登记项目，不写记录
        c.execute("INSERT INTO projects(name, agent, created) VALUES('空项目','t','2026-10-01')")
    p = brain.get_project(hub_root, "空项目")
    assert p["total"] == 0 and p["shown"] == 0 and p["records"] == []


def test_limit_param_and_cap(hub_root):
    """④ limit 可调，但夹在 1..100（不给调用方一次拉 10 万条）。"""
    name = _seed(hub_root, 30)
    assert brain.get_project(hub_root, name, limit=10)["shown"] == 10
    assert brain.get_project(hub_root, name, limit=0)["shown"] == 1
    assert brain.get_project(hub_root, name, limit=9999)["shown"] == 30   # 只有 30 条
    assert brain.get_project(hub_root, name, limit=9999)["total"] == 30


def test_superseded_not_counted(hub_root):
    """已取代的记录不进 total（与列表口径一致，否则「共 N 条」永远对不上）。"""
    name = _seed(hub_root, 6)
    with brain.db_conn(hub_root) as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM records WHERE project=? ORDER BY id", (name,))]
        c.execute("UPDATE records SET status='superseded', superseded_by=? WHERE id=?",
                  (ids[-1], ids[0]))
    p = brain.get_project(hub_root, name)
    assert p["total"] == 5, p
