# -*- coding: utf-8 -*-
"""R830：曝光免疫断路器 + 采纳回流排序。

为什么改（R829 台账遗留 1）：
  R829 把「曝光」和「采纳」分成了两本账，但只是**能看见**——`inject_count` 高、
  `adopt_count`=0 的记忆照样被推。真实库 #9 靠每次心跳被推 84 次、零采纳，还带着
  「2026-10-14 复查」这种早已过期的期限；继续推只会训练模型忽略整块注入
  （mem9：每轮塞同款文字会让模型免疫；Nlm Memory 因此默认关逐句注入）。

本文件钉住四件事：
  1. 推 ≥5 次、零采纳、30 天内的记忆**不再进被动注入通道**（逐句回忆 + 开工推送）；
  2. 主动检索（hub_search 那条路）**照旧能查到**它——断路器只管「别塞给模型」；
  3. 被证实采纳过的不冷却；凉够 30 天的自动放行（自愈，不是永久封杀）；
  4. 采纳量参与排序：同等命中下，被用上过的那条排前面。

「修之前会怎样失败」：第 1 条用例在 HEAD 版上必红——旧代码里 `prompt_recall` 完全不看
`inject_count`，被推 5 次零采纳的那条照样出现在 `memory_ids` 里。
"""
import datetime

import brain


def _seed(root, content, **kw):
    brain.init_db(root)
    return brain.add_memory(root, content, **kw)


def _push_n(root, mid, n=5, ts=""):
    """造 n 次被动曝光（走生产写入口 _bump_inject，不手改库）。"""
    for _ in range(n):
        with brain.db_conn(root) as conn:
            conn.execute("UPDATE memories SET inject_count=inject_count+1, last_inject=? "
                         "WHERE id=?", (ts or brain._now(), mid))


def _ask(root, prompt="pyinstaller 打包 _meipass 路径在哪", session="r830"):
    return brain.prompt_recall(root, prompt, agent="workbuddy", cwd=root,
                               session=session, update_state=False)


STRONG = ("PyInstaller onefile 打包后，--add-data 的文件在 sys._MEIPASS 根目录，"
          "不在子目录；用 CArchiveEntry 读 toc 可以静态核对。")


def test_muted_memory_not_injected(hub_root):
    """核心验收：推 5 次零采纳 → 不再进逐句注入（旧代码这里必红）。"""
    mid = _seed(hub_root, STRONG, kind="lesson", tags="pyinstaller,_MEIPASS", agent="qoder")
    res = _ask(hub_root, session="r830-a")
    assert mid in res["memory_ids"], "前提不成立：第一次就召回不到，后面的断言没意义"

    _push_n(hub_root, mid, 5)
    res2 = _ask(hub_root, session="r830-b")
    assert mid not in res2["memory_ids"], res2["text"]
    assert res2["muted"] >= 1, res2


def test_muted_still_reachable_by_active_search(hub_root):
    """断路器只管「别主动塞」：主动检索必须照旧查得到，否则等于把知识删了。"""
    mid = _seed(hub_root, STRONG, kind="lesson", tags="pyinstaller,_MEIPASS")
    _push_n(hub_root, mid, 6)
    assert mid not in _ask(hub_root, session="r830-c")["memory_ids"]
    rows = brain.search_memories(hub_root, "pyinstaller _MEIPASS", limit=5)
    assert mid in [r["id"] for r in rows], [r["id"] for r in rows]


def test_adopted_memory_never_muted(hub_root):
    """被证实采纳过（adopt_count>0）→ 不冷却，它是有用的结论。"""
    mid = _seed(hub_root, STRONG, kind="lesson", tags="pyinstaller,_MEIPASS")
    _push_n(hub_root, mid, 8)
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE memories SET adopt_count=1 WHERE id=?", (mid,))
    res = _ask(hub_root, session="r830-d")
    assert mid in res["memory_ids"], res["text"]
    assert res["muted"] == 0


def test_cooling_window_self_heals(hub_root):
    """凉够 30 天 → 自动放行一次，不是永久封杀。"""
    mid = _seed(hub_root, STRONG, kind="lesson", tags="pyinstaller,_MEIPASS")
    old = (datetime.datetime.now() - datetime.timedelta(days=31)
           ).strftime("%Y-%m-%dT%H:%M:%S")
    _push_n(hub_root, mid, 5, ts=old)
    res = _ask(hub_root, session="r830-e")
    assert mid in res["memory_ids"], res["text"]


def test_recall_for_skips_muted(hub_root):
    """开工推送（心跳三层）也走同一道闸——它才是最频繁的那条通道（推一次就 +1）。"""
    mid = _seed(hub_root, STRONG, kind="lesson", tags="pyinstaller",
                project="演示-免疫R830", pinned=True)
    _push_n(hub_root, mid, 5)
    rows = brain.recall_for(hub_root, "演示-免疫R830", agent="workbuddy")
    assert mid not in [r["id"] for r in rows], [r["id"] for r in rows]
    # 置顶不豁免：pin 是优先级，不是「永远出现」的通行证（真实库 #9 就是置顶却零采纳）
    assert rows == [] or mid not in [r["id"] for r in rows]


def test_new_memory_without_timestamp_not_muted(hub_root):
    """`last_inject` 为空的新记忆不能被连带静音。

    这是 SQL 里最容易写错的一处：`NULL>=x` 在 SQLite 是 NULL，`NOT(cond AND NULL)` 还是
    NULL → WHERE 当假 → 整行被丢掉。用 COALESCE(last_inject,'') 才挡得住。
    """
    mid = _seed(hub_root, STRONG, kind="lesson", tags="pyinstaller,_MEIPASS")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE memories SET inject_count=5, last_inject='' WHERE id=?", (mid,))
    res = _ask(hub_root, session="r830-f")
    assert mid in res["memory_ids"], res["text"]


def test_adoption_boosts_ranking(hub_root):
    """同等命中下，被用上过的那条排前面（采纳回流排序）。"""
    brain.init_db(hub_root)
    # 两条内容必须真的不同：_fold_near_dupes 会把近似重复折成一条，那样就测不出排序了。
    plain = brain.add_memory(hub_root, "PyInstaller onefile 打包后，--add-data 的文件在 "
                             "sys._MEIPASS 根目录，不在子目录。", kind="lesson",
                             tags="pyinstaller,_MEIPASS")
    used = brain.add_memory(hub_root, "PyInstaller 打包产物用 CArchiveEntry 读 toc，可以"
                            "静态核对哪些模块进了包里。", kind="lesson",
                            tags="pyinstaller,_MEIPASS")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE memories SET adopt_count=3 WHERE id=?", (used,))
    res = _ask(hub_root, session="r830-g")
    ids = res["memory_ids"]
    assert used in ids and plain in ids, ids
    assert ids.index(used) < ids.index(plain), ids
