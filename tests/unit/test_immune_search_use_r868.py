# -*- coding: utf-8 -*-
"""R868：主动搜索过的记忆不该被曝光免疫静音。

病根（真库实锤）：曝光免疫只问「推了 5 次、采纳 0 次」，不看 `use_count`。
#113 被推 5 次零采纳、但被 hub_search/hub_memory_read 主动查了 24 次，仍被判
「推了也没用」；#164（use 4）、#221（use 17）同款。真库 `muted_total=3`，
两条查询各被静音 1 条——其中一条正是被反复主动搜索的接入抽象结论。

口径：主动搜索不是「采纳」，但它比被动曝光强；use_count ≥ 3 足以推翻
「推了也没用」。SQL 通道（`_immune_sql`）与 Python 通道（`is_exposure_immune`）
必须给同一条记忆相同判定。
"""
import brain


def _seed(root, content, **kw):
    brain.init_db(root)
    return brain.add_memory(root, content, **kw)


def _push_n(root, mid, n=5, ts=""):
    for _ in range(n):
        with brain.db_conn(root) as conn:
            conn.execute("UPDATE memories SET inject_count=inject_count+1, last_inject=? "
                         "WHERE id=?", (ts or brain._now(), mid))


def _use(root, mid, n):
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE memories SET use_count=? WHERE id=?", (n, mid))


def _ask(root, session):
    return brain.prompt_recall(root, "pyinstaller 打包 _meipass 路径在哪",
                               agent="workbuddy", cwd=root, session=session,
                               update_state=False)


STRONG = ("PyInstaller onefile 打包后，--add-data 的文件在 sys._MEIPASS 根目录，"
          "不在子目录；用 CArchiveEntry 读 toc 可以静态核对。")


def test_active_search_unmutes(hub_root):
    """use_count>=3 → 不再进冷却，逐句回忆照旧能召回。"""
    mid = _seed(hub_root, STRONG, kind="lesson", tags="pyinstaller,_MEIPASS", agent="qoder")
    _push_n(hub_root, mid, 5)
    _use(hub_root, mid, 3)
    res = _ask(hub_root, session="r868-a")
    assert mid in res["memory_ids"], res["text"]
    assert res["muted"] == 0, res


def test_low_use_still_muted(hub_root):
    """use_count=2 不到阈值 → 仍按 R830 静音，不能放宽成「搜过一次就免死」。"""
    mid = _seed(hub_root, STRONG, kind="lesson", tags="pyinstaller,_MEIPASS")
    _push_n(hub_root, mid, 5)
    _use(hub_root, mid, 2)
    res = _ask(hub_root, session="r868-b")
    assert mid not in res["memory_ids"], res["text"]
    assert res["muted"] >= 1, res


def test_pure_function_boundary():
    now = brain.datetime.datetime(2026, 11, 4, 12, 0, 0)
    base = {"inject_count": 5, "adopt_count": 0,
            "last_inject": "2026-11-03T12:00:00"}
    assert brain.is_exposure_immune(dict(base, use_count=2), now) is True
    assert brain.is_exposure_immune(dict(base, use_count=3), now) is False
    assert brain.is_exposure_immune(dict(base, use_count=0, adopt_count=1), now) is False
    # 没有 last_inject → 不猜（沿用 R830：不能把新记忆连带静音）
    assert brain.is_exposure_immune({"inject_count": 5, "adopt_count": 0}, now) is False


def test_recall_for_and_stats_agree(hub_root):
    """开工推送与治理统计要同一口径：高 use 的放行，低 use 的仍静音。"""
    hot = _seed(hub_root, STRONG, kind="lesson", tags="pyinstaller",
                project="演示-免疫R868", pinned=True)
    cold = _seed(hub_root, "PyInstaller 打包产物的 CArchiveEntry 静态核对方法，"
                           "可以看哪些模块进了包。", kind="lesson", tags="pyinstaller",
                 project="演示-免疫R868", pinned=True)
    _push_n(hub_root, hot, 5)
    _push_n(hub_root, cold, 5)
    _use(hub_root, hot, 5)
    rows = brain.recall_for(hub_root, "演示-免疫R868", agent="workbuddy")
    ids = [r["id"] for r in rows]
    assert hot in ids, ids
    assert cold not in ids, ids

    st = brain.exposure_vs_adoption(hub_root)
    muted = [r["id"] for r in st["muted"]]
    assert cold in muted, muted
    assert hot not in muted, muted
    assert st["muted_total"] == 1, st
