# -*- coding: utf-8 -*-
"""R857 · B061：`errors.sig` 在生产上是空的，「签名逐字相同」这条强通道一次也没生效过。

R856 修通了口径（登记侧不再被说明文字带偏、账本不再重解析去重键），但收尾量库发现下一层空转：
`errors` 表 20 行里 **19 行 sig 是空的**（这列 R843 才建），唯一带值的 #20 存的还是旧口径污染值。
`_error_covered` 的第一道判据是「欠账键 == errors.sig」，等式两边一边有值一边永远空——那几条
判成 resolved 走的**全是**模糊 LIKE 通道，R834 承诺的「登记一次、下次同签名秒命中」在数据上没落地。
`error_add` 也不会自愈：它只在「合并且 sig 为空」时补写。

所以本工具做一次性点名回填，四条判据都各有用例：
  ① 空 sig 一律补；② 非空 sig 默认**不动**，要覆盖必须点名（改前值是别人算的）；
  ③ 抽不出键的行必须**报出来**而不是静默消失（静默消失=分母谎，R856 同族）；
  ④ 每写一行留一条流水写明改前值（回滚路径），写完欠账走等值通道闭合。
"""
import sys
from pathlib import Path

import brain
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import err_sig_backfill as tool  # noqa: E402

ERR = "sqlite3.OperationalError: no such column: created_at"
FIX = "把 created_at 换成 ts（该库时间列就叫 ts）"


def _seed(hub_root, title="查 errors 表写了 created_at", detail=ERR + "（查 brain.db 的 errors 表）"):
    brain.init_db(hub_root)
    info = brain.error_add(hub_root, "qoder", title, detail=detail, undo=FIX,
                           return_info=True)
    return int(info["id"])


def _blank_sig(hub_root, *ids):
    with brain.db_conn(hub_root) as conn:
        for i in ids:
            conn.execute("UPDATE errors SET sig='' WHERE id=?", (i,))


def test_plan_fills_empty_sig(hub_root):
    eid = _seed(hub_root)
    _blank_sig(hub_root, eid)                        # 模拟 R843 之前建的历史行
    items = tool.plan(hub_root, [])
    row = next(x for x in items if x["id"] == eid)
    assert row["act"] == "fill" and row["old"] == "", row
    assert "created_at" in row["new"], row


def test_plan_will_not_touch_a_non_empty_sig_unless_named(hub_root):
    """非空值是有人算过的——默认不动，覆盖必须逐条点名。"""
    eid = _seed(hub_root)
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE errors SET sig='something|old' WHERE id=?", (eid,))
    row = next(x for x in tool.plan(hub_root, []) if x["id"] == eid)
    assert row["act"] == "needs-naming", row
    named = next(x for x in tool.plan(hub_root, [eid]) if x["id"] == eid)
    assert named["act"] == "overwrite", named


def test_apply_writes_and_leaves_the_rollback_trail(hub_root):
    eid = _seed(hub_root)
    _blank_sig(hub_root, eid)
    items = tool.plan(hub_root, [])
    n = tool.apply(hub_root, items, "qoder")
    assert n == 1, n
    with brain.db_conn(hub_root) as conn:
        sig = conn.execute("SELECT sig FROM errors WHERE id=?", (eid,)).fetchone()["sig"]
        logs = list(conn.execute("SELECT note FROM journal WHERE action='错误签名'"))
    assert sig and "created_at" in sig, sig
    assert len(logs) == 1 and "(空)" in logs[0]["note"], [l["note"] for l in logs]


def test_backfilled_sig_is_what_the_equality_channel_looks_up(hub_root):
    """这才是 B061 的验收物：`_error_covered` 第一道判据那句
    `SELECT 1 FROM errors WHERE sig=?` **查得到行**——不是「模糊通道也能过」。

    顺序照生产来：先撞（此刻库里没答案，欠账落账），再登记（R856 之后算的是新口径键，
    等值通道立刻对得上），然后把这行改回历史形状（旧口径污染值 / 空值），等值通道断，
    回填一次又接上。中间那步「断」就是真库 19 行空 sig 的处境。
    """
    brain.init_db(hub_root)
    key = brain.error_sig_of(ERR)
    brain.error_recall(hub_root, ERR, tool="Bash", agent="qoder", session="s1",
                       state_path=str(Path(hub_root) / "s.json"))
    eid = _seed(hub_root)
    with brain.db_conn(hub_root) as conn:
        assert conn.execute("SELECT 1 FROM errors WHERE sig=?", (key,)).fetchone(), key
        # 退回历史形状：19 行空 sig + 1 行旧口径污染值
        conn.execute("UPDATE errors SET sig='' WHERE id=?", (eid,))
        assert conn.execute("SELECT 1 FROM errors WHERE sig=?", (key,)).fetchone() is None
    tool.apply(hub_root, tool.plan(hub_root, []), "qoder")
    with brain.db_conn(hub_root) as conn:
        got = conn.execute("SELECT sig FROM errors WHERE id=?", (eid,)).fetchone()
    assert got["sig"] == key, (got, key)


def test_rows_without_a_key_are_reported_not_dropped(hub_root):
    """抽不出键的行必须出现在清单里（静默消失就是把分母吃掉，R856 同族）。"""
    brain.init_db(hub_root)
    brain.error_add(hub_root, "qoder", "部署文档第 3 步目录名写错了", detail="改成 output 重跑就通了")
    items = tool.plan(hub_root, [])
    assert len(items) == 1 and items[0]["act"] == "skip-nokey", items
    assert [x for x in items if x["act"] == "fill"] == [], items


def test_historical_key_we_cannot_recompute_is_left_alone(hub_root):
    """旧 sig 还在、按新口径却抽不出键：保留旧值并**点名报出**，不清空也不覆盖。"""
    eid = _seed(hub_root)
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE errors SET detail='', title='部署文档第 3 步目录名写错了', "
                     "sig='old|key' WHERE id=?", (eid,))
    items = tool.plan(hub_root, [eid])
    assert [(x["act"], x["old"]) for x in items] == [("keep-old", "old|key")], items
    assert tool.apply(hub_root, items, "qoder") == 0
    with brain.db_conn(hub_root) as conn:
        assert conn.execute("SELECT sig FROM errors WHERE id=?", (eid,)).fetchone()["sig"] == "old|key"


def test_backup_snapshot_is_a_real_database(hub_root):
    """写前整库在线备份——没有它，一次批量写错只能靠逐行 note 回滚（不够，回填会连改多行）。"""
    brain.init_db(hub_root)
    dest = Path(tool._backup(hub_root))               # noqa: SLF001
    assert dest.exists() and dest.stat().st_size > 0
    import sqlite3
    with sqlite3.connect(str(dest)) as c:
        assert c.execute("SELECT COUNT(*) FROM errors").fetchone()[0] == 0
