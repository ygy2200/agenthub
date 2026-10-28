# -*- coding: utf-8 -*-
"""幽灵相似度扫描提速的两条等价保证（R235）。

背景：真实库（551 条近 90 天记录 / 373 条记忆）上 `_ghost_memory` 单项 1.03s，
占满整轮体检耗时的近三分之一。剖析定位到两处：
① `_tokens` 每个词段一次 `[a-z0-9]+` 正则全匹配（9.7 万次调用）；
② O(N²) 里的 `len(ta & tb)` 集合交——单个项目 367 条就是 6.7 万对，448ms。
改法是「纯等价替换」：小写化后用 `isascii() and isalnum()` 判拉丁词；把 token
集合编码成整型位集，交集大小换成 `(a & b).bit_count()`。没有近似、没有采样、
没有改阈值语义，所以门禁必须是**逐对结果一致**，而不是「快了就行」。
"""
import random
import re

import brain

_REF_WORD = re.compile(r"[a-z0-9]+")


def _ref_tokens(text):
    """R235 之前的 `_tokens` 原样（正则全匹配版），当参照实现。"""
    text = re.sub(r"[^\w一-鿿]+", " ", (text or "").lower())
    toks = set()
    for seg in text.split():
        if _REF_WORD.fullmatch(seg):
            toks.add(seg)
        else:
            toks.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return toks


def _ref_sub_tokens(title, content):
    return _ref_tokens(re.sub(r"【[^】]*】", " ", f"{title} {content}"))


def _ref_similar_memories(root, threshold, limit):
    """R235 之前的 similar_memories 循环骨架（集合交版），只取决定结果的 (a, b, sim)。"""
    with brain.db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, kind, content FROM memories WHERE "
            + brain._live_sql() + " ORDER BY id")]
    toks = [(r["id"], _ref_tokens(r["content"])) for r in rows]
    out = []
    for i in range(len(toks)):
        ta = toks[i][1]
        if not ta:
            continue
        sa = len(ta)
        for j in range(i + 1, len(toks)):
            tb = toks[j][1]
            if not tb:
                continue
            sb = len(tb)
            lo, hi = (sa, sb) if sa < sb else (sb, sa)
            if lo / hi < threshold:
                continue
            inter = len(ta & tb)
            sim = inter / (sa + sb - inter)
            if sim >= threshold:
                out.append((toks[i][0], toks[j][0], round(sim, 2)))
            if len(out) >= max(1, min(int(limit), 50)):
                return out
    out.sort(key=lambda x: -x[2])
    return out


def _ref_similar_records(root, threshold, limit, days=90):
    """R235 之前的 similar_records 循环骨架（同项目分组 + 集合交版）。"""
    mark = "（auto-hook）"
    with brain.db_conn(root) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, project, title, content FROM records WHERE "
            + brain._live_sql() + " AND date >= date('now', ?) AND source NOT LIKE 'migrated:%' "
            "AND source != 'auto' ORDER BY id", (f"-{int(days)} days",))]
    rows = [r for r in rows if mark not in (r["title"] or "")]
    by = {}
    for r in rows:
        by.setdefault(r["project"], []).append(r)
    out = []
    for items in by.values():
        toks = [(r, _ref_sub_tokens(r["title"], r["content"])) for r in items]
        for i in range(len(toks)):
            ta = toks[i][1]
            if not ta:
                continue
            sa = len(ta)
            for j in range(i + 1, len(toks)):
                tb = toks[j][1]
                if not tb:
                    continue
                sb = len(tb)
                lo, hi = (sa, sb) if sa < sb else (sb, sa)
                if lo / hi < threshold:
                    continue
                inter = len(ta & tb)
                sim = inter / (sa + sb - inter)
                if sim >= threshold:
                    out.append((toks[i][0]["id"], toks[j][0]["id"], round(sim, 2)))
                if len(out) >= max(1, min(int(limit), 50)):
                    out.sort(key=lambda x: -x[2])
                    return out
    out.sort(key=lambda x: -x[2])
    return out


def _trim(pairs):
    return [(p["a"], p["b"], p["sim"]) for p in pairs]


def test_tokens_matches_pre_r235_reference():
    """分词快路径必须与正则全匹配版逐条同解——含下划线/全角标点/中英混排/数字。"""
    corpus = [
        "修了 FlClash 的规则，7890 端口在监听",
        "deploy_diff 11/11 一致（target=D:\\agent资料）",
        "snake_case_name 与 camelCaseName 混排 ABCdef123",
        "【目的】【做了什么】【验证结果】【如何回滚】",
        "纯英文 words only here",
        "中文无标点长句一口气写完没有空格",
        "emoji 🎉 与全角：；「」『』括号",
        "  前后空白  与  多个   连续   空格  ",
        "", "   ", None, "0", "_", "__a__", "a1B2",
    ]
    for text in corpus:
        assert brain._tokens(text) == _ref_tokens(text), repr(text)


def test_bits_index_intersection_is_exact_pairwise():
    """位集 popcount 与集合交逐对严格相等（含 >64 位跨字节、空集、同集）。"""
    rng = random.Random(20261027)
    pool = [f"t{i}" for i in range(400)]
    sets = []
    for _ in range(40):
        sets.append(set(rng.sample(pool, rng.randint(0, 120))))
    sets.append(set())
    bits, sizes = brain._bits_index(sets)
    assert sizes == [len(s) for s in sets]
    assert all(b.bit_count() == len(s) for b, s in zip(bits, sets))
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            assert (bits[i] & bits[j]).bit_count() == len(sets[i] & sets[j]), (i, j)


def test_bits_index_vocab_beyond_one_byte_and_empty():
    """词表跨过字节边界时编码不串位；全空词表退化为 0 而不报错。"""
    many = set(f"tok{i}" for i in range(300))
    bits, sizes = brain._bits_index([many, set(many)])
    assert sizes == [300, 300]
    assert (bits[0] & bits[1]).bit_count() == 300
    other = set(f"tok{i}" for i in range(150, 450))
    b2, _ = brain._bits_index([many, other])
    assert (b2[0] & b2[1]).bit_count() == len(many & other) == 150
    assert brain._bits_index([set(), set()]) == ([0, 0], [0, 0])


def _seed_brain(root):
    """固定语料：同项目近似重复对 + 跨项目同文（不该比）+ 纯模板头（空词集）+ 自动记录。"""
    brain.init_db(root)
    ids = []
    ids.append(brain.add_memory(root, "部署目录在 D:/old/path，打包用 build.bat 一条命令出 exe",
                                kind="fact", agent="t"))
    ids.append(brain.add_memory(root, "部署目录在 D:/old/path，打包用 build.bat 一条命令出 exe 文件",
                                kind="fact", agent="t"))
    ids.append(brain.add_memory(root, "代理端口是 7890，FlClash 未运行时出网类操作全失败",
                                kind="lesson", agent="t"))
    ids.append(brain.add_memory(root, "完全不同的话题：液压传动考试复习要点整理成 Word 文档",
                                kind="note", agent="t"))
    ids.append(brain.add_memory(root, "【】【】", kind="note", agent="t"))
    for n in range(12):
        ids.append(brain.add_memory(root, f"巡检第 {n} 轮记录：三层门禁跑完，部署一致性核对通过",
                                    kind="project", agent="t"))

    today = "2026-10-27"
    for n in range(8):
        brain.add_record(root, "甲-项目", "t", today, f"R{n} 收口",
                         f"【目的】提速 {n}\n【做了什么】改分词与位集交集\n"
                         f"【验证结果】真实库 551 条记录逐对等价\n【如何回滚】git 回退 brain.py")
    brain.add_record(root, "甲-项目", "t", today, "R9 收口",
                     "【目的】提速 9\n【做了什么】改分词与位集交集\n"
                     "【验证结果】真实库 551 条记录逐对等价\n【如何回滚】git 回退 brain.py")
    brain.add_record(root, "乙-项目", "t", today, "R9 收口",
                     "【目的】提速 9\n【做了什么】改分词与位集交集\n"
                     "【验证结果】真实库 551 条记录逐对等价\n【如何回滚】git 回退 brain.py")
    brain.add_record(root, "甲-项目", "t", today, "会话锚（auto-hook）", "同文但不该参与比较")
    brain.add_record(root, "甲-项目", "t", today, "自动捕获", "同文但不该参与比较", source="auto")
    brain.add_record(root, "甲-项目", "t", today, "纯模板头", "【目的】【做了什么】【验证结果】【如何回滚】")


def test_similar_memories_matches_reference_across_thresholds(hub_root):
    _seed_brain(hub_root)
    for thr in (0.3, 0.45, 0.55, 0.7, 0.9, 1.0):
        for lim in (1, 2, 5, 50):
            got = _trim(brain.similar_memories(hub_root, thr, lim))
            ref = _ref_similar_memories(hub_root, thr, lim)
            assert got == ref, f"thr={thr} lim={lim} got={got} ref={ref}"
    assert brain.similar_memories(hub_root, 1.5, 10) == []


def test_similar_records_matches_reference_across_thresholds(hub_root):
    _seed_brain(hub_root)
    for thr in (0.3, 0.45, 0.55, 0.7, 0.9, 1.0):
        for lim in (1, 2, 5, 50):
            got = _trim(brain.similar_records(hub_root, thr, lim))
            ref = _ref_similar_records(hub_root, thr, lim)
            assert got == ref, f"thr={thr} lim={lim} got={got} ref={ref}"
    assert brain.similar_records(hub_root, 1.5, 10) == []


def test_record_filters_still_hold_after_speedup(hub_root):
    """提速不许顺手改口径：跨项目同文不配、auto/auto-hook 仍被排除、空词集不配。"""
    _seed_brain(hub_root)
    pairs = brain.similar_records(hub_root, threshold=0.3, limit=50)
    with brain.db_conn(hub_root) as conn:
        title = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM records")}
        proj = {r["id"]: r["project"] for r in conn.execute("SELECT id, project FROM records")}
    for p in pairs:
        assert proj[p["a"]] == proj[p["b"]], p
        assert "auto-hook" not in title[p["a"]] and "auto-hook" not in title[p["b"]], p
        assert p["project"] == proj[p["a"]] == proj[p["b"]], p
    assert all(p["a"] != p["b"] for p in pairs)


def test_ghost_memory_check_reports_same_candidates(hub_root):
    """ops 层结论（组数 + 明细首行指向的 id 对）与参考实现一致。"""
    import ops
    _seed_brain(hub_root)
    r = ops.run_check("ghost_memory", "", hub_root)
    mem = _trim(brain.ghost_memory_candidates(hub_root, threshold=0.55, limit=10))
    rec = _trim(brain.ghost_record_candidates(hub_root, threshold=0.55, limit=10))
    total = len(mem) + len(rec)
    assert r["ok"] == (total == 0), (r, mem, rec)
    if total:
        # 明细只展示前 8 行（截断是展示层的事）；断言口径：计数对得上、展示的对都在参考集里
        assert f"记忆 {len(mem)} + 记录 {len(rec)}" in r["summary"], r["summary"]
        shown = {(a, b) for a, b, _sim in mem + rec}
        pairs = re.findall(r"(记忆|记录) #(\d+) ↔ #(\d+)", "\n".join(r["detail"]))
        assert pairs, r["detail"]
        for _kind, a, b in pairs:
            assert (int(a), int(b)) in shown or (int(b), int(a)) in shown, (_kind, a, b, shown)
