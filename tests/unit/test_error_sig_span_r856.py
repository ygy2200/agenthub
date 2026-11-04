# -*- coding:utf-8 -*-
"""R856 · B060：报错欠账的「登记 → 下次同签名命中」回环在生产上是断的。

R834 把这条写成了不变量：「召回侧的 seeds 用同一个 `error_sig_of`，所以同一条报错的键
**必然**逐字相同——这是『登记一次，下次同签名秒命中』的实现，不是概率」。两处都只保证
了**函数**同一个，没保证**输入形状**同一个，真库当场打脸（2026-11-04 实锤）：

  · 召回侧：钩子递给 `error_recall` 的只有报错原文；
  · 登记侧：`hub_report_error` 按自家提示词的写法收 title（一句人话）+ detail（报错原文
    **和我后来怎么修的**）——`error_sig_of` 从整段散文里抽词，说明文字里的 `brain.db`、
    `last_ts`、`table_info` 一样带下划线、一样算标识符，于是挤进 `_error_sig_key` 的前 4。

结果 errors#20 的 sig = `brain.db|created_at|last_detail|last_ts`，欠账那边是
`no such column: created_at|...`：等值通道不相等，退到模糊判定又被第二个断点挡住——
`error_debt` 把 `searches.query` 里存的**去重键**（`a|b` 拼起来的）当报错正文再喂一次
`error_signature_terms`，抽出一个跨着 `|` 的超长 w=3 词，LIKE 谁也匹配不上。
两条合起来：**登记了答案，欠账照样 open，下次同签名照样不会秒命中**。

本文件每条都先在旧码上红：
  ①② 登记侧签名不许被说明文字带偏（同一句报错，掺不掺注释都得是同一个键）；
  ③ 真库形状：按 ops 提示的写法登记 → `error_debt` 里那条从 open 变 resolved；
  ④ 账本侧不许把去重键当正文重解析（拆出来的词里不能有 `|`）；
  ⑤ 反证：整段都没有错误形状时不许收窄成空（不能因为修签名把「没形状」判成「没签名」）；
  ⑥ 反证：只有真报错那行参与签名，同一条报错的两次不同措辞登记仍然互相认得出（合并通道）。
"""
import brain

RAW = "sqlite3.OperationalError: no such column: created_at"
# 主人/agent 登记时的真实形状：报错原文一行，后面跟着「为什么会这样 + 怎么修」
PROSE_DETAIL = (
    "sqlite3.OperationalError: no such column: created_at（查 _hub/brain.db 的 errors 表时"
    "直接写了 created_at）。真因：errors 表的时间列叫 ts，没有 created_at，"
    "完整列名：id, ts, agent, project, title, detail, undo, status, repeat_count, "
    "last_ts, last_detail, sig。"
)
TITLE = "查 errors 表写了 created_at——该库时间列是 ts，先 pragma table_info 再写 SQL"


def test_sig_of_ignores_prose_around_the_error(hub_root):
    """掺了说明的 detail 与裸报错原文，必须算出同一个签名键。"""
    assert brain.error_sig_of(RAW) == brain.error_sig_of(PROSE_DETAIL)


def test_sig_key_has_no_prose_identifiers(hub_root):
    """说明文字里的 `last_ts`/`table_info` 不是这条报错的身份，不许进键。"""
    key = brain.error_sig_of(PROSE_DETAIL)
    assert key, "裸报错抽不出签名才是问题，这里应当有键"
    for polluted in ("last_ts", "last_detail", "table_info", "brain.db", "created_at（"):
        assert polluted not in key, f"签名被说明文字带偏：{key}"


def test_annotation_brackets_are_prose_too(hub_root):
    """真库 errors#8 的形状：`no such table: distill_seen 【销账2026-10-02】`。

    【】《》是人在标注，ASCII 报错正文不会带——不收进剪除集，销账批注就会进键，
    下次同一条报错（没带批注的现场）又对不上。
    """
    raw = "sqlite3.OperationalError: no such table: distill_seen"
    annotated = raw + " 【销账 2026-10-02，见 R841】"
    assert brain.error_sig_of(annotated) == brain.error_sig_of(raw), brain.error_sig_of(annotated)


def test_debt_closes_after_prose_registration(hub_root, tmp_path):
    """按 ops 的提示写法登记一次，欠账要自己收编——这是 B060 唯一算数的验收。"""
    st = str(tmp_path / "s.json")
    brain.init_db(hub_root)
    r = brain.error_recall(hub_root, RAW, tool="Bash", agent="qoder",
                           session="s1", state_path=st)
    assert r["error_ids"] == [] and r["memory_ids"] == [], r
    debt = brain.error_debt(hub_root, days=7)
    assert debt["open"] >= 1, debt
    assert not debt["resolved"], "登记前不该有已收编"

    added = brain.error_add(hub_root, "qoder", TITLE, detail=PROSE_DETAIL,
                            project="演示", undo="把 created_at 换成 ts",
                            return_info=True)
    assert added["id"], added
    after = brain.error_debt(hub_root, days=7)
    assert after["resolved"] >= 1, after
    assert after["open"] == debt["open"] - 1, (debt["open"], after["open"])
    row = next(x for x in after["rows"] if x["status"] == "resolved")
    assert "created_at" in row["sig"], row


def test_debt_terms_come_from_splitting_the_key(hub_root):
    """账本读的是去重键（`a|b` 形状），不许当报错正文重解析出跨分隔符的长词。"""
    brain.init_db(hub_root)
    key = brain.error_sig_of(RAW)
    terms = brain._debt_sig_terms(key)
    assert terms and all("|" not in t for t, _ in terms), terms
    # 拆出来的词应当就是键的那几段本身
    assert sorted(t for t, _ in terms) == sorted(p for p in key.split("|") if p)


def test_debt_detail_leads_with_the_identity_word(hub_root):
    """账本 detail 只有 70 字预算：招牌词得排在路径段前面，否则读账本的人看不见是哪条错。

    R856 自己踩的：按 `|` 拆开后保持键里的字母序，`ops._error_debt` 拼出来是
    `brain_error_recall brain_error_recall.mjs d:/…/brain_error_recall.mjs`——截断正好
    把 `enoent` 挤没，R834 那条「欠账要在 ops 里点得名」的用例当场红。顺序不能影响判定，
    所以这里同时钉住「换个顺序算出来的键一样」。
    """
    brain.init_db(hub_root)
    enoent = ("Error: ENOENT: no such file or directory, open "
              "'D:/agent资料/hooks/brain_error_recall.mjs'")
    key = brain.error_sig_of(enoent)
    terms = brain._debt_sig_terms(key)
    assert "enoent" in " ".join(t for t, _ in terms)[:70], (key, terms)
    assert brain._error_sig_key(terms) == brain._error_sig_key(list(reversed(terms)))


def test_old_shape_key_is_a_third_state_not_debt(hub_root):
    """旧算法写进去的正文形状键（真库实测：`https://memorypapers.org/` 这类检索词）判不了，
    既不许算成「已收编」（假装没问题），也不许算成「欠账」（催一条根本没法登记的账）。

    R856 修完在真库看到的：窗口里 4 条签名拆不出任何词，全是修复前的残留——新代码不再
    产生它们，但它们还要在 7 天窗口里挂几天。三态记录让读数说得清「几条欠、几条判不了」。
    """
    brain.init_db(hub_root)
    legacy = "https://memorypapers.org/"
    assert brain._debt_sig_terms(legacy) == [], brain._debt_sig_terms(legacy)
    brain._log_error_miss(hub_root, "qoder", legacy, [])
    d = brain.error_debt(hub_root, days=7)
    assert d["asked"] == 1, d
    assert d["open"] == 0 and d["unjudgeable"] == 1, d
    row = d["rows"][0]
    assert row["status"] == "unjudgeable", row


def test_distinct_sigs_are_reported_separately_from_the_sample(hub_root):
    """欠账条数是在**取回的那批**上判的，命中率用的是全窗口计数——两个口径不写清，
    「没答案 1 条」会变成「窗口共 40 类但我只判了前 8 类」的谎（R834 台账 §四④ 同族）。
    """
    brain.init_db(hub_root)
    for i in range(12):
        brain._log_error_miss(hub_root, "qoder", f"sigaaa{i:02d}|zzz", [("sigaaa%02d" % i, 3)])
    d = brain.error_debt(hub_root, days=7, limit=5)
    assert len(d["rows"]) == 5, d
    assert d["distinct"] == 12, d
    assert d["sampled"] == 5, d
    # 没签名的失败连「欠账」都挂不上（rows 里被跳过），distinct 也不该把它算成「没判的」
    brain._log_error_miss(hub_root, "qoder", "", [])
    assert brain.error_debt(hub_root, days=7, limit=5)["distinct"] == 12


def test_no_shape_text_is_not_narrowed_to_empty(hub_root):
    """整段没有错误形状时原样参与签名——不能因为收窄把「没形状」变成「没词」。"""
    plain = "部署文档里第 3 步写错了目录名，改成 output 之后重跑一次就通了"
    assert brain._error_span(plain) == plain
    assert brain.error_sig_of("完全没有任何错误形状的一句话") == ""


def test_same_fault_two_wordings_still_match(hub_root):
    """同一条报错、两种现场措辞（钩子 payload / 人工登记）签名必须一致，否则合并通道白设。"""
    hook_shape = ("失败调用：python -c \"...\"\n"
                  "Traceback (most recent call last):\n"
                  '  File "<string>", line 1, in <module>\n'
                  + RAW + "\nExit code 1")
    a = brain.error_sig_of(brain._error_voice(hook_shape))
    b = brain.error_sig_of(PROSE_DETAIL)
    assert a == b, (a, b)
