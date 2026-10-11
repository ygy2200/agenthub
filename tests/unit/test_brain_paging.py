# -*- coding: utf-8 -*-
"""unit 层：records 分页切片（P2-3a 虚拟滚动数据源）。不启 Qt。"""


def _seed(root, n: int, projects: int = 5):
    import brain
    brain.init_db(root)
    with brain.db_conn(root) as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO records(project,agent,date,title,content,source,created,"
                "valid_from,record_type) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"P{i % projects}", "zcode", f"2026-10-{1 + i % 9:02d}", f"r{i}", f"c{i}",
                 "hub", "2026-10-11T00:00:00", "2026-10-11", "work_log"))
        for k in range(projects):
            conn.execute("INSERT OR IGNORE INTO projects(name,created) VALUES(?,?)",
                         (f"P{k}", "2026-10-11"))


def test_records_page_slices_without_gap_or_dup(tmp_path):
    import brain
    root = str(tmp_path)
    _seed(root, 250)
    assert brain.records_count(root) == 250
    p1, total = brain.records_page(root, 0, 100)
    p2, _ = brain.records_page(root, 100, 100)
    p3, _ = brain.records_page(root, 200, 100)
    assert total == 250
    assert (len(p1), len(p2), len(p3)) == (100, 100, 50)
    ids = [r["id"] for r in p1 + p2 + p3]
    assert len(set(ids)) == 250, "分页出现重复/遗漏"
    # 排序口径与 list_records 一致（首条 id 相同）
    assert p1[0]["id"] == brain.list_records(root, limit=1)[0]["id"]


def test_records_page_filters_push_down(tmp_path):
    import brain
    root = str(tmp_path)
    _seed(root, 100, projects=5)  # 每个项目 20 条
    rows, total = brain.records_page(root, 0, 100, project="P2")
    assert total == 20 and len(rows) == 20
    assert all(r["project"] == "P2" for r in rows)
    assert brain.records_count(root, project="P2") == 20
    rows_a, total_a = brain.records_page(root, 0, 100, agent="zcode")
    assert total_a == 100
    rows_none, total_none = brain.records_page(root, 0, 100, agent="不存在")
    assert total_none == 0 and rows_none == []


def test_records_page_edges(tmp_path):
    import brain
    root = str(tmp_path)
    _seed(root, 30)
    # offset 超界 → 空页但 total 正确
    rows, total = brain.records_page(root, 10 ** 6, 100)
    assert rows == [] and total == 30
    # 非法参数兜底
    rows2, _ = brain.records_page(root, -5, -1)
    assert rows2 == []
    # records_agents 去重（小写）
    _seed2 = brain.records_agents(root)
    assert _seed2 == ["zcode"]


def test_records_page_excludes_deleted(tmp_path):
    import brain
    root = str(tmp_path)
    _seed(root, 10)
    rid = brain.list_records(root, limit=1)[0]["id"]
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE records SET status='deleted' WHERE id=?", (rid,))
    _, total = brain.records_page(root, 0, 100)
    assert total == 9, "软删记录不应计入分页"
