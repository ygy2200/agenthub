# -*- coding: utf-8 -*-
r"""R236·df 单次扫描的两条保证：机制等值 + 活跃口径修正。

背景：真实库上 `search_memories` 热态 75ms 里 `_coverage_gate` 占 48ms，其中 df 一段
34–82ms。旧写法**每个 query 词对每张表发一条全表 `content LIKE` COUNT**（16 词 = 32 次
扫），`stale_health` 的 5 条泄漏探针各发一次检索，于是该项 602ms、把 `stale_health` 顶成
整轮体检耗时榜首。R236 把每表压成一条条件聚合 `SUM(content LIKE ?)` 一次算完全部 token。

换 SQL 形态的风险是「悄悄改变 df」→ 悄悄改变 idf → 悄悄改变闸门判定。所以门禁不是
「快了就行」，而是：① 逐 token 与**参照实现**（逐词 WHERE 查询）全等；② 闸门留/丢哪些行
在参照 df 下逐查询一致；③ LIKE 通配符转义仍成立（`%`/`_`/`\` 不许多匹配）。

第二条是本轮顺带查出的**口径缺陷**：`_token_df` 的 docstring 一直承诺「只算活跃行」，
SQL 却没有 live 谓词——被 superseded/作废/过期的行照样进 df。df 虚高 → 该词 idf 被压低
→ 「蹭常见词」更容易过关。现在两表各带 `_rec_live_sql()` / `_live_sql()`。
"""
import re

import brain
import pytest

TOKS = ["专词", "存在", "在的", "100%", "50_30", r"路径\子", "zzz"]

# 语料（前 6 条记忆 / 后 4 条记录）：m2/m3 与 r2/r3 是三种「已失效」形态
MEM_ROWS = [
    "专词甲 常见词 存在",              # m1 活跃
    "专词乙 被作废 存在 在的",          # m2 status=superseded
    "专词丙 已过期 在的",              # m3 valid_until 过期
    "",                                # m4 空 content（memories.content NOT NULL）
    r"100% 覆盖率 50_30 路径\子",      # m5 通配符本体
    "1000 覆盖率 50x30 路径x子",       # m6 诱饵：转义失效时会被误匹配
]
REC_ROWS = [
    "专词甲 的存在",                   # r1 活跃
    "",                                # r2 活跃，随后 UPDATE 成 NULL
    "专词戊 存在 在的",                # r3 superseded_by 指过去
    "废话 在的 在的",                  # r4 valid_until 过期
]
LIVE_DF = {"专词": 2, "存在": 2, "在的": 0, "100%": 1, "50_30": 1, r"路径\子": 1, "zzz": 0}
STALE_DF = {"专词": 5, "存在": 4, "在的": 4, "100%": 1, "50_30": 1, r"路径\子": 1, "zzz": 0}


def _seed(hub_root):
    brain.init_db(hub_root)
    mids = [brain.add_memory(hub_root, c, kind="fact", agent="t") for c in MEM_ROWS]
    rids = [brain.add_record(hub_root, "甲-项目", "t", "2026-01-01", f"标题{i}", c)
            for i, c in enumerate(REC_ROWS)]
    with brain.db_conn(hub_root) as c:
        c.execute("UPDATE memories SET status='superseded' WHERE id=?", (mids[1],))
        c.execute("UPDATE memories SET valid_until='2020-01-01' WHERE id=?", (mids[2],))
        c.execute("UPDATE records SET content=NULL WHERE id=?", (rids[1],))
        c.execute("UPDATE records SET superseded_by=? WHERE id=?", (rids[0], rids[2]))
        c.execute("UPDATE records SET valid_until='2020-01-01' WHERE id=?", (rids[3],))
    return hub_root


def _ref_df(root, tokens, live=True):
    """参照实现：逐 token 一条 WHERE COUNT（R236 之前的 SQL 形态）。

    live=True 用与实现相同的每表活跃谓词（证「条件聚合 == 逐词过滤」机制等值）；
    live=False 复刻 R236 之前的真实口径（不带 live 谓词），用来对照被修掉的缺陷。
    """
    df = {t: 0 for t in tokens}
    with brain.db_conn(root) as c:
        for tbl, pred in (("records", brain._rec_live_sql()), ("memories", brain._live_sql())):
            where = f"({pred}) AND" if live else ""
            for t in tokens:
                n = c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {where} content LIKE ?"
                              " ESCAPE '\\'", (f"%{brain._like_escape(t)}%",)).fetchone()[0]
                df[t] += int(n or 0)
    return df


def _ref_total(root):
    with brain.db_conn(root) as c:
        return (c.execute(f"SELECT COUNT(*) FROM records WHERE {brain._rec_live_sql()}").fetchone()[0]
                + c.execute(f"SELECT COUNT(*) FROM memories WHERE {brain._live_sql()}").fetchone()[0])


def test_batched_df_equals_per_token_reference(hub_root):
    _seed(hub_root)
    assert brain._token_df(hub_root, TOKS) == _ref_df(hub_root, TOKS)


def test_batched_df_values_are_the_expected_ones(hub_root):
    """把等值测试钉在具体数字上：参照实现若一起写错，纯等值断言会假绿。"""
    _seed(hub_root)
    df, total = brain._token_stats(hub_root, TOKS)
    assert df == LIVE_DF, df
    assert total == _ref_total(hub_root) == 6      # 活跃 mem 4 条 + 活跃 rec 2 条


def test_like_wildcards_stay_escaped(hub_root):
    r"""`%`/`_`/`\` 按字面匹配：诱饵行（1000 / 50x30 / 路径x子）不许进 df。"""
    _seed(hub_root)
    assert brain._token_df(hub_root, ["100%", "50_30", r"路径\子"]) == {"100%": 1, "50_30": 1,
                                                                        r"路径\子": 1}


def test_live_predicate_excludes_stale_rows(hub_root):
    """口径修正的直接证据：同一批 token，活跃口径严格小于旧的无过滤口径。"""
    _seed(hub_root)
    live = brain._token_df(hub_root, TOKS)
    stale = _ref_df(hub_root, TOKS, live=False)
    assert stale == STALE_DF, stale
    assert all(live[t] <= stale[t] for t in TOKS)
    assert live["专词"] < stale["专词"] and live["在的"] < stale["在的"]


def test_repeated_tokens_counted_once(hub_root):
    """旧写法按 token 累加，重复词翻倍；批量写法先去重，重复传入与单次传入必须同值。"""
    _seed(hub_root)
    assert brain._token_df(hub_root, ["存在"] * 3) == brain._token_df(hub_root, ["存在"]) == {"存在": 2}


def test_empty_tokens_short_circuit(hub_root):
    _seed(hub_root)
    assert brain._token_stats(hub_root, []) == ({}, 0)
    assert brain._token_stats(hub_root, None) == ({}, 0)


class _Spy:
    """记录实际发出的语句，守住「每表一条聚合」这个本轮动机本身。

    注意 `db_conn` 是 contextmanager 对象，必须先 `__enter__` 拿到真连接——
    直接对它调 `.execute` 会得到 `'_GeneratorContextManager' has no attribute`。
    """

    def __init__(self, cm, sink):
        self._cm, self._sink, self._c = cm, sink, None

    def __enter__(self):
        self._c = self._cm.__enter__()
        return self

    def __exit__(self, *a):
        return self._cm.__exit__(*a)

    def execute(self, sql, params=()):
        self._sink.append((re.sub(r"\s+", " ", sql.strip()), params))
        return self._c.execute(sql, params)


def _spy(monkeypatch, sink):
    real = brain.db_conn
    monkeypatch.setattr(brain, "db_conn", lambda root, *a, **k: _Spy(real(root, *a, **k), sink))


def _shape(sink):
    return ([s for s, _ in sink if s.startswith("SELECT SUM")],
            [s for s, _ in sink if s.startswith("SELECT COUNT(*)")])


def test_single_scan_statement_shape(hub_root, monkeypatch):
    _seed(hub_root)
    sink = []
    _spy(monkeypatch, sink)
    brain._token_stats(hub_root, TOKS)             # 7 个词：一块就够
    aggs, tots = _shape(sink)
    assert len(aggs) == 2, sink                    # 每表一条聚合（旧写法是 14 条 COUNT）
    assert len(tots) == 2, sink                    # 语料总量各表一条
    assert "FROM records" in aggs[0] and "FROM memories" in aggs[1], aggs
    assert "status='active'" in aggs[0] and "status='active'" in aggs[1], aggs   # live 谓词没漏
    assert len(sink[0][1]) == len(TOKS)            # 参数个数 = 词数，不是 2× 词数


def test_chunking_beyond_64_tokens(hub_root, monkeypatch):
    """>64 词分块：块数 = ⌈n/64⌉ × 2 表，语料总量仍只两条。"""
    _seed(hub_root)
    toks = [f"tok{i}" for i in range(200)]
    sink = []
    _spy(monkeypatch, sink)
    df, total = brain._token_stats(hub_root, toks)
    aggs, tots = _shape(sink)
    assert len(aggs) == 8 and len(tots) == 2, sink
    assert len(df) == 200 and total == _ref_total(hub_root)


def test_fail_open_on_unreadable_db(tmp_path):
    """读不到库必须返回 ({}, 0)：闸门退化为纯计数，检索不因加权失败而整体报错。"""
    assert brain._token_stats(str(tmp_path / "never-created"), TOKS) == ({}, 0)


GATE_ROWS = [{"id": 1, "kind": "fact", "title": "甲", "content": "专词甲 常见词 存在", "tags": "", "project": "p"},
             {"id": 2, "kind": "fact", "title": "乙", "content": "在的 在的 废话", "tags": "", "project": "p"},
             {"id": 3, "kind": "fact", "title": "丙", "content": r"100% 覆盖率 50_30 专词", "tags": "", "project": "p"},
             {"id": 4, "kind": "fact", "title": "丁", "content": "zzz 与本题无关", "tags": "", "project": "p"},
             {"id": 5, "kind": "fact", "title": "戊", "content": "存在 在的 专词 废话", "tags": "", "project": "p"}]
QUERIES = ["专词甲 存在 在的", "在的 存在 废话", "100% 覆盖率 50_30",
           "zzz 不存在 qwerty", "专词 存在 在的 废话 覆盖率"]


def _gate_ids(root, kw, rows=None):
    return [r["id"] for r in brain._coverage_gate(root, [dict(x) for x in (rows or GATE_ROWS)], kw)]


def test_gate_keeps_and_drops_identical_under_reference_df(hub_root, monkeypatch):
    """闸门留/丢哪些行必须与参照 df（逐词 WHERE 版）下逐查询一致——本轮全部风险的落点。"""
    _seed(hub_root)
    with_batched = {q: _gate_ids(hub_root, q) for q in QUERIES}
    monkeypatch.setattr(brain, "_token_stats",
                        lambda r, t: (_ref_df(r, list(dict.fromkeys(t))), _ref_total(r)))
    with_reference = {q: _gate_ids(hub_root, q) for q in QUERIES}
    assert with_batched == with_reference, (with_batched, with_reference)
    assert 1 in with_batched["专词甲 存在 在的"] and 2 not in with_batched["专词甲 存在 在的"]


def test_garbage_query_still_cleared(hub_root):
    """df 换成批量口径后，乱输查询（拉丁零命中）依旧清零，而不是降弱留下。"""
    _seed(hub_root)
    rows = [{"id": 91, "kind": "fact", "title": "噪", "content": "存在 在的", "tags": "", "project": "p"},
            {"id": 92, "kind": "fact", "title": "噪", "content": "在的 废话", "tags": "", "project": "p"}]
    assert _gate_ids(hub_root, "qwertyzzz 不存在 在的", rows) == []


def test_gate_without_df_degrades_to_pure_count(hub_root, monkeypatch):
    """df 读不到时按「命中 ≥2 词」放行（规则④），与加权前行为一致。"""
    _seed(hub_root)
    monkeypatch.setattr(brain, "_token_stats", lambda r, t: ({}, 0))
    assert _gate_ids(hub_root, "在的 存在 废话") == [2, 5]
