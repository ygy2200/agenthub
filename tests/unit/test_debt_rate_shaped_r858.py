# -*- coding:utf-8 -*-
"""R858 · B063：报错命中率的分母里掺着「根本不是报错的触发」。

`ops._error_debt` 的 `rate = answered*100//asked`，而
`asked = answered + miss_rows`、`miss_rows` 是**所有** `error_recall_miss` 行——
包括 `(no_signature)`（钩子被触发、但正文抽不出错误形状）。分子 `answered` 只数
`tool='error_recall' AND hits>0`，也就是「有签名且有货」。同一个比率里两种人群：
真库近 7 天 22 条 miss 里 **12 条是 `(no_signature)`**（55%），于是报出来 13%——
这个数既不能说「大脑不行」（一半触发压根不是报错），也不能当趋势用（口径随
钩子触发面漂）。R856 修 `distinct/sampled` 是同一族：分子分母必须是同一个人群。

修法不是把 `(no_signature)` 丢掉（那是钩子的真实触发量，看得见才有下一步），
是**分开报**：命中率只算有形状的触发，无形状的触发单独说一句。

每条都先在旧码上红。
"""
import re

import brain
import ops

ROOT_MARK = "searches_error_hits"


def _seed(hub_root, answered=1, keyed=1, unshaped=3):
    """直接写 searches：绕开钩子去重/预算，只造计数形状。

    行必须落在口径锚点**之后**（`error_debt` 的双闸门是 窗口 ∧ 锚点，`ts>=floor`），
    所以先落锚点、再用明确的时间戳写行——同一秒内 `strftime('now')` 会贴着锚点抖动。
    """
    import datetime
    brain.init_db(hub_root)
    brain.mark_caliber(hub_root, ROOT_MARK, "本用例自己落锚点")
    ts = (datetime.datetime.now() + datetime.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    with brain.db_conn(hub_root) as conn:
        for _ in range(answered):
            conn.execute("INSERT INTO searches(ts,agent,tool,query,hits) VALUES (?,?,?,?,?)",
                         (ts, "qoder", "error_recall", "x", 1))
        for _ in range(keyed):
            conn.execute("INSERT INTO searches(ts,agent,tool,query,hits) VALUES (?,?,?,?,?)",
                         (ts, "qoder", "error_recall_miss",
                          "operationalerror|no such column: created_at", 0))
        for _ in range(unshaped):
            conn.execute("INSERT INTO searches(ts,agent,tool,query,hits) VALUES (?,?,?,?,?)",
                         (ts, "qoder", "error_recall_miss", "(no_signature)", 0))


def test_debt_splits_shaped_from_unshaped_triggers(hub_root):
    """账本要能分别回答：撞了几次可识别的报错、又被非报错触发了几次。"""
    _seed(hub_root, answered=1, keyed=1, unshaped=3)
    d = brain.error_debt(hub_root, days=7)
    assert d["miss_keyed"] == 1 and d["miss_unshaped"] == 3, d
    assert d["asked"] == 5, "asked 仍是总触发量（钩子真被叫了 5 次），不许偷偷改口径"
    assert d["asked_shaped"] == 2, "命中率的分母应当只有 1 命中 + 1 有签名欠账"


def test_ops_rate_uses_the_shaped_denominator(hub_root):
    """报出来的百分比必须是「有形状的触发」上的比率：掺 3 条非报错会把 50% 说成 20%。"""
    _seed(hub_root, answered=1, keyed=1, unshaped=3)
    r = ops.run_check("error_debt", root=hub_root)
    assert r["ok"] is not None
    head = r["summary"]
    assert "命中率 50%" in head, head          # 1/(1+1)，不是 1/5
    assert "非报错" in head and "3" in head, "无形状的触发要单列，不能蒸发也不能进分母"


def test_no_shaped_trigger_does_not_report_a_rate(hub_root):
    """全是非报错触发时不许报「命中率 0%」——那是把钩子触发面当成知识缺口。"""
    _seed(hub_root, answered=0, keyed=0, unshaped=4)
    d = brain.error_debt(hub_root, days=7)
    assert d["asked_shaped"] == 0, d
    head = ops.run_check("error_debt", root=hub_root)["summary"]
    # 「不进命中率」这句说明本身带那个词，所以断言的是**没有报出数字**，不是没有提到它
    assert not re.search(r"命中率 \d", head), head
