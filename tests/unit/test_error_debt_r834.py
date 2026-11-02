# -*- coding: utf-8 -*-
"""R834 报错欠账可见化 + 签名沉淀（B041）。

复盘实锤（output/迭代台账-R834-2026-11-02.md §一）：12 条线上形状用例里
`A_absent=5 / B_gate=0 / C_trunc=0`——**检索不是瓶颈，库里没写才是**；而 `no_match`
分支直接 return，连「大脑被问了但答不上」这一行都不落，于是既没有分母、也没有登记入口。
本轮把两件事钉住：

  1) miss 必须落账（searches + journal 同签名去重），欠账要能聚合、能被收编、能被一条
     ops 检查项查到（不给 80 个工具面再添一个新名字）；
  2) 登记一次就要下次同签名秒命中——签名沉淀进 `errors.sig`，`error_recall` 的 errors
     通道按 sig 精确匹配；`add_memory` 只给含错误形状的正文补签名 tag，偏好类逐字不变。

外加 R833 遗留 #7 的机制化解法：口径变更落 meta 时间戳，跨变更点的比率不许再被历史假零行污染
（本轮自己就先信了那个 55.6%，见台账 §一①）。
"""
import sqlite3

import pytest


@pytest.fixture
def hub(tmp_path):
    """干净 tmp 大脑。用例一律传 session=""（skey 为空 → 不读不写 ~/.workbuddy 的会话去重状态），
    所以本文件的用例既不碰真库、也不碰真状态文件。"""
    root = tmp_path / "hub"
    root.mkdir()
    import brain
    brain.init_db(str(root))
    return str(root)


# 2026-11-02 线上真撞到的两条形状（journal/searches 抓回，非合成文本）
ENOENT_MJS = ("Error: ENOENT: no such file or directory, open "
              "'D:/agent资料/hooks/brain_error_recall.mjs'")
GBK_ERR = ("UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713' in "
           "position 42: illegal multibyte sequence")
# 第三条：全大写错误常量，不带编号/点划/空格——模糊闸门判它「不成招牌」，
# 但它恰恰是生产副本探针第一轮 FAIL 的那条形状（台账 §三）。
ECONN = "Error: connect ECONNREFUSED 127.0.0.1:7890"


def _rows(root, sql, args=()):
    import brain
    with brain.db_conn(root) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _recall(root, text, agent="qoder", session=""):
    import brain
    return brain.error_recall(root, text, tool="Bash", agent=agent,
                              session=session, update_state=False)


# ─────────────────────────── 1) miss 落账 ───────────────────────────

def test_miss_writes_journal_and_search_rows(hub):
    """库里没答案时必须留下「被问过」的证据，否则永远算不出空转率、也不知道该登记什么。"""
    res = _recall(hub, ENOENT_MJS)
    assert res["skipped"] == "no_match", res
    j = _rows(hub, "SELECT * FROM journal WHERE action='brain_error_miss'")
    assert len(j) == 1, j
    assert j[0]["target"] == res["sig"], "target 必须是去重签名，欠账才聚合得起来"
    s = _rows(hub, "SELECT * FROM searches WHERE tool='error_recall_miss'")
    assert len(s) == 1 and int(s[0]["hits"]) == 0, s
    assert s[0]["query"] == res["sig"], "流水按签名存，聚合才是同一条欠账"


def test_repeat_miss_same_sig_counts_instead_of_piling(hub):
    """同签名撞 4 次 = 一条欠账 ×4，不是四行——审计链只追加不回改，重复次数走 searches。"""
    first = _recall(hub, ENOENT_MJS)
    for _ in range(3):
        _recall(hub, ENOENT_MJS)
    j = _rows(hub, "SELECT * FROM journal WHERE action='brain_error_miss'")
    assert len(j) == 1, j
    assert j[0]["target"] == first["sig"]
    s = _rows(hub, "SELECT * FROM searches WHERE tool='error_recall_miss'")
    assert len(s) == 4, s
    assert _debt(hub)["rows"][0]["count"] == 4, _debt(hub)


def test_no_signature_miss_logs_search_only(hub):
    """「命令失败了」这种抽不出签名的失败：进分母（searches），不进欠账（没签名可追）。"""
    res = _recall(hub, "命令失败了")
    assert res["skipped"] == "no_signature"
    assert _rows(hub, "SELECT * FROM journal WHERE action='brain_error_miss'") == []
    s = _rows(hub, "SELECT * FROM searches WHERE tool='error_recall_miss'")
    assert len(s) == 1 and "(no_signature)" in s[0]["query"], s


def test_observe_only_run_does_not_log_miss(hub):
    """count=False 是冒烟探针的护身符：试跑造的假欠账会把 R830 的免疫断路器喂脏。"""
    import brain
    before = _rows(hub, "SELECT COUNT(*) AS n FROM journal")
    brain.error_recall(hub, ENOENT_MJS, tool="Bash", agent="probe",
                       session="", update_state=False, count=False)
    after = _rows(hub, "SELECT COUNT(*) AS n FROM journal")
    assert before[0]["n"] == after[0]["n"], "试跑不许写账"


# ─────────────────────────── 2) 欠账账本 ───────────────────────────

def test_error_debt_lists_open_miss(hub):
    _recall(hub, ENOENT_MJS)
    d = _debt(hub)
    assert d["open"] == 1, d
    row = d["rows"][0]
    assert row["status"] == "open" and row["count"] == 1
    assert "enoent" in row["sig"].lower()
    assert row["terms"], "欠账要带签名词，登记人才知道该往库里写什么"


def _debt(root, days=7, agent=""):
    import brain
    return brain.error_debt(root, days=days, agent=agent)


def test_error_debt_closes_when_knowledge_lands(hub):
    """收编判定 = 同一套精度闸门全库扫——库里现在有能过闸门的答案，欠账就该自动消掉。"""
    import brain
    _recall(hub, ENOENT_MJS)
    assert _debt(hub)["open"] == 1
    brain.error_add(hub, "qoder", "钩子脚本没挂载，报错即时回忆整条静默",
                    detail=ENOENT_MJS, undo="cli.py hook-install 重装钩子")
    d = _debt(hub)
    assert d["open"] == 0, d
    assert d["resolved"] == 1, d


def test_error_debt_agent_filter_and_window(hub):
    _recall(hub, ENOENT_MJS, agent="qoder")
    assert _debt(hub, agent="workbuddy")["open"] == 0
    assert _debt(hub, agent="qoder")["open"] == 1
    # 窗口用「人为造一条 30 天前的流水」来验，不用 days=0：ts 存的是秒级文本，
    # days=0 时 floor 与刚写入那行同秒，`>=` 恰好把它算进来——判据会随跑测时刻抖。
    assert _debt(hub, days=31)["open"] == 1
    import brain
    with brain.db_conn(hub) as conn:
        conn.execute("UPDATE searches SET ts=strftime('%Y-%m-%dT%H:%M:%S','now','-30 day')"
                     " WHERE tool='error_recall_miss'")
    assert _debt(hub, days=7)["open"] == 0, "30 天前的欠账不该还挂在简报脸上"


def test_miss_injects_at_the_existing_spot_once(hub, tmp_path):
    """主人 2026-11-02 定的口径：欠账就写在**原来提醒的那个位置**（报错即时回忆自己那段），
    不开新入口、不进简报——而且同签名同会话只催一次，不许一直提醒。"""
    state = str(tmp_path / "state.json")
    first = brain_err_recall(hub, ENOENT_MJS, session="s1", state_path=state)
    assert first["skipped"] == "debt_hint", first      # 说了，但只说欠账——不是「大脑答对了」
    t = first["text"]
    assert t, "答不上时不再全静默：欠账就写在原来提醒的那个位置"
    assert "大脑没" in t and "hub_report_error" in t, t
    assert 'hub_ops_run(name="error_debt")' in t, "提示要给出查账本的那条路"
    again = brain_err_recall(hub, ENOENT_MJS, session="s1", state_path=state)
    assert again["text"] == "", again                   # 同一签名不催第二次
    assert again["skipped"] == "no_match", again
    other = brain_err_recall(hub, ENOENT_MJS, session="s2", state_path=state)
    assert "大脑没" in other["text"], other


def test_debt_hint_does_not_steal_the_recall_slot(hub, tmp_path):
    """两本去重账必须分开（R834 实锤的坑）：欠账说过一次之后，同会话里
    「登记 → 再撞」这条回环仍要能说话——那正是我们最想让它看见的一刻。"""
    import brain
    state = str(tmp_path / "state.json")
    hint = brain_err_recall(hub, ENOENT_MJS, session="s1", state_path=state)
    assert "大脑没" in hint["text"], hint
    brain.error_add(hub, "qoder", "钩子脚本没挂载，报错即时回忆整条静默",
                    detail=ENOENT_MJS, undo="cli.py hook-install")
    hit = brain_err_recall(hub, ENOENT_MJS, session="s1", state_path=state)
    assert hit["text"] and "登记过同类故障" in hit["text"], hit
    assert hit["skipped"] == "", hit
    third = brain_err_recall(hub, ENOENT_MJS, session="s1", state_path=state)
    assert third["skipped"] == "seen_this_session" and third["text"] == "", third


def brain_err_recall(root, text, session="", state_path=""):
    import brain
    return brain.error_recall(root, text, tool="Bash", agent="qoder",
                              session=session, state_path=state_path)


def test_no_signature_stays_silent(hub):
    """抽不出签名的失败（「命令失败了」）没有可登记的东西，硬提醒纯属噪音。"""
    res = _recall(hub, "命令失败了")
    assert res["skipped"] == "no_signature" and res["text"] == ""


def test_brief_gains_no_line_from_debt(hub, tmp_path):
    """防自己过度设计：欠账不新开简报面——有 miss 前后的简报必须逐字一致。"""
    import brain
    before = brain.session_brief(hub, agent="qoder")["text"]
    brain.error_recall(hub, ENOENT_MJS, tool="Bash", agent="qoder",
                       session="s9", state_path=str(tmp_path / "st.json"))
    brain.error_recall(hub, GBK_ERR, tool="Bash", agent="qoder",
                       session="s9", state_path=str(tmp_path / "st.json"))
    assert brain.session_brief(hub, agent="qoder")["text"] == before


# ─────────────────────────── 3) 签名沉淀（B041）───────────────────────────

def test_errors_table_gains_sig_column(hub):
    cols = {r["name"] for r in _rows(hub, "PRAGMA table_info(errors)")}
    assert "sig" in cols, cols


def test_error_add_sediments_signature_terms(hub):
    """登记时把签名抽出来存好：靠 title/detail 的 prose 碰运气是 R833 那套，不是沉淀。"""
    import brain
    info = brain.error_add(hub, "qoder", "钩子脚本没挂载，报错即时回忆整条静默",
                           detail=ENOENT_MJS, undo="cli.py hook-install",
                           return_info=True)
    row = _rows(hub, "SELECT * FROM errors WHERE id=?", (info["id"],))[0]
    assert row["sig"], row
    assert "enoent" in row["sig"].lower() and "brain_error_recall" in row["sig"].lower(), row["sig"]


def test_registering_an_error_makes_the_next_occurrence_hit(hub):
    """端到端（本轮主指标）：A_absent 案例登记一次 → 同签名再来必须 E_hit 且指向那行。"""
    before = _recall(hub, ENOENT_MJS)
    assert before["skipped"] == "no_match" and not before["error_ids"]
    info = _err(hub, "钩子脚本没挂载，报错即时回忆整条静默", ENOENT_MJS)
    after = _recall(hub, ENOENT_MJS, session="other")
    assert after["skipped"] == "" and after["text"], after
    assert info["id"] in after["error_ids"], after
    assert "登记过同类故障" in after["text"]


def _err(root, title, detail):
    import brain
    return brain.error_add(root, "qoder", title, detail=detail, undo="重装钩子",
                           return_info=True)


def test_sig_matching_survives_prose_only_titles(hub):
    """标题全中文、detail 也不含被问的那句原文时，仍靠 sig 里的签名词认出来。"""
    import brain
    info = brain.error_add(hub, "qoder", "部署后钩子跑的是旧副本，改动没生效",
                           detail="另一件事的现场：ENOENT 缺 brain_error_recall.mjs，已按 hook-install 复原",
                           undo="重跑 cli.py deploy + hook-install", return_info=True)
    hit = _rows(hub, "SELECT sig FROM errors WHERE id=?", (info["id"],))[0]["sig"]
    assert "brain_error_recall" in hit, hit


def test_signature_identity_beats_the_fuzzy_gate(hub):
    """全大写错误常量（ECONNREFUSED）不带编号也不带点划，`_err_distinctive` 不认它当招牌，
    模糊判定整条False——可 `errors.sig` 是同一个函数算出来的键，两边相等是「同一条报错」
    的身份证据，不是「像」。生产副本探针实锤：只走模糊判定时，登记完欠账仍报 open
    （③ 6→6）、再撞仍只出提示（④ skipped=debt_hint），本轮承诺的「登记 → 立刻回环」
    在这类签名上是空的。"""
    import brain
    terms = brain.error_signature_terms(ECONN)
    assert terms and not any(brain._err_distinctive(t) for t, w in terms if w >= 3), terms
    assert brain._err_accept(terms, ECONN.lower()) is False, "前提：模糊闸门确实不认这条"
    assert _recall(hub, ECONN)["skipped"] == "no_match"
    info = _err(hub, "代理没起，本地 7890 连不上", ECONN)
    row = next((r for r in brain.error_debt(hub, days=7)["rows"]
                if r["sig"] == brain.error_sig_of(ECONN)), None)
    assert row and row["status"] == "resolved", "登记后必须自己收编"
    hit = _recall(hub, ECONN, session="other")
    assert hit["skipped"] == "" and info["id"] in hit["error_ids"], hit
    assert "登记过同类故障" in hit["text"]


def test_exact_sig_row_still_needs_a_body(hub):
    """签名相同但 detail 和 undo 都是空的行不算「库里已有答案」——空登记不许把欠账销掉，
    否则账本会越记越绿，而模型拿到的还是一句废话。"""
    import brain
    _recall(hub, ECONN)
    sig = brain.error_sig_of(ECONN)
    _rows(hub, "INSERT INTO errors(ts,agent,title,detail,undo,status,sig)"
               " VALUES('2026-11-02T10:00:00','qoder','空登记','','','open',?)", (sig,))
    assert brain._error_covered(hub, brain.error_signature_terms(sig)) is False
    assert _recall(hub, ECONN, session="other")["error_ids"] == []


def test_add_memory_sediments_tags_for_error_shapes(hub):
    import brain
    mid = brain.add_memory(hub, f"Windows 控制台先设 PYTHONIOENCODING=utf-8，"
                                f"否则 {GBK_ERR.split(':')[0]} 会把 ✓ 打不出来。",
                           kind="lesson", tags="编码", agent="qoder")
    tags = _rows(hub, "SELECT tags FROM memories WHERE id=?", (mid,))[0]["tags"]
    assert "编码" in tags, tags                       # 显式 tag 必须原样保留
    assert "unicodeencodeerror" in tags.lower(), tags  # 签名词自动补进来
    assert "pythonioencoding" in tags.lower(), tags


def test_add_memory_leaves_plain_preference_tags_untouched(hub):
    """偏好类正文没有错误形状，硬塞英文签名词只会把标签库变成垃圾场。"""
    import brain
    mid = brain.add_memory(hub, "用户偏好：汇报只讲做了什么/验证结果/怎么回滚，"
                                "交付前自检照跑但不在回复里输出闸门快照。",
                           kind="preference", tags="汇报,自检", agent="qoder")
    row = _rows(hub, "SELECT tags FROM memories WHERE id=?", (mid,))[0]
    assert {t.strip() for t in row["tags"].split(",")} == {"汇报", "自检"}, row["tags"]


def test_add_memory_caps_sedimented_tags(hub):
    """一条长 traceback 能抽出十几个签名词，标签面必须有上限，否则 tag 库被报错刷满。"""
    import brain
    long_err = ("Traceback (most recent call last):\n"
                '  File "D:/a/b.py", line 12, in main\n'
                "    subprocess.run(cmd, encoding='utf-8')\n"
                "TypeError: 'NoneType' object is not iterable: "
                "fused_score error_recall_state brain_error_recall.sqlite3 "
                "OperationalError no such column: fused_score")
    mid = brain.add_memory(hub, long_err, kind="lesson", tags="", agent="qoder")
    tags = [t for t in _rows(hub, "SELECT tags FROM memories WHERE id=?", (mid,))[0]["tags"].split(",")
            if t.strip()]
    assert len(tags) <= 6, tags


# ─────────────────────────── 4) 口径时间戳（遗留 #7）───────────────────────────

def test_mark_caliber_is_idempotent_and_readable(hub):
    import brain
    ts1 = brain.mark_caliber(hub, "searches_error_hits", "hits 含记录/错误（R833）")
    ts2 = brain.mark_caliber(hub, "searches_error_hits", "重复调用不许改写锚点")
    assert ts1 == ts2 == brain.caliber_ts(hub, "searches_error_hits"), (ts1, ts2)


def test_debt_rate_ignores_pre_caliber_rows(hub):
    """历史假零行必须被锚点挡在外面——本轮就是先信了 55.6% 那个被污染的比率才走弯的。"""
    import brain
    with brain.db_conn(hub) as conn:
        conn.execute("INSERT INTO searches(ts,agent,tool,query,hits) "
                     "VALUES('2020-01-01T00:00:00','qoder','error_recall','老口径',0)")
    brain.mark_caliber(hub, "searches_error_hits", "hits 含记录/错误（R833）")
    _recall(hub, ENOENT_MJS)                 # 一条真 miss
    _recall(hub, GBK_ERR)                    # 库里没答案 → 也是一条 miss
    d = _debt(hub)
    assert d["asked"] == 2 and d["answered"] == 0, d
    with brain.db_conn(hub) as conn:
        n_legacy = conn.execute("SELECT COUNT(*) AS n FROM searches WHERE ts<'2021-01-01'").fetchone()["n"]
    assert n_legacy == 1 and d["skipped_legacy"] == 1, d


def test_error_debt_survives_empty_hub(hub):
    d = _debt(hub)
    assert d["open"] == 0 and d["rows"] == [] and d["asked"] == 0


# ─────────────────── 5) 出口：ops 检查项 + 流水分账（不进工具面）───────────────────

def test_ops_check_lists_the_miss_without_crying_wolf(hub):
    """欠账的查询出口走 ops 检查项：`hub_ops_list` 里看得见、`hub_ops_run` 一条命令查得到，
    不再给 80 个工具的被动上下文添一个新名字。一次性撞的不报红——长期红灯等于没人看的灯。"""
    import ops
    assert "error_debt" in ops.CHECKS and "error_debt" in ops.PATROL_CHECKS
    r = ops.run_check("error_debt", root=hub)
    assert r["ok"] is True and "没有欠账" in r["summary"], r
    _recall(hub, ENOENT_MJS)
    r = ops.run_check("error_debt", root=hub)
    assert r["ok"] is True, r                          # 撞一次不催
    assert "enoent" in " ".join(r["detail"]).lower(), r


def test_ops_check_red_only_when_the_same_sig_keeps_biting(hub):
    """同签名重复撞 ≥DEBT_REPEAT_FAIL 次仍没答案 = 反复撞的坑，这时才该红灯。"""
    import ops
    for _ in range(ops.DEBT_REPEAT_FAIL):
        _recall(hub, ENOENT_MJS)
    r = ops.run_check("error_debt", root=hub)
    assert r["ok"] is False and "没答案" in r["summary"], r
    assert "error_debt" in ops.CHECK_FIXES, "红灯必须自带可执行的下一步"


def test_hint_is_not_journaled_as_a_real_delivery(hub, tmp_path, capsys):
    """流水分两种动作名：真命中记 brain_error_recall（送达率/采纳回填吃它），
    欠账提示记 brain_error_debt_hint——混一类就把「大脑没答案」统计成「大脑答对了」。"""
    import brain_error_recall as ber
    assert ber.main(["--root", hub, "--agent", "qoder", "--tool", "Bash",
                     "--session", "s1", "--state", str(tmp_path / "st.json"),
                     "--error", ENOENT_MJS]) == 0
    out = capsys.readouterr().out
    assert "大脑没" in out, out                         # 提示照样送到（原来那个位置）
    acts = {r["action"] for r in _rows(hub, "SELECT action FROM journal")}
    assert "brain_error_debt_hint" in acts, acts
    assert "brain_error_recall" not in acts, acts
