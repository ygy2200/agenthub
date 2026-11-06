# -*- coding: utf-8 -*-
r"""R929：签名键的前 4 位按权重排，不按字母排；存量 errors.sig 保守重签。

修之前会怎样失败（2026-11-06 真库取证，`%TEMP%/sig_drift_r929b.py`）：
`_error_sig_key` 先把 terms 截到 6 个，再 `sorted()` **按字母**排完只取前 4。而
`error_signature_terms` 已经按 (-权重, 词) 排过——字母重排把「认出这条报错靠哪个词」
这一维信息整段丢掉：w=3 的招牌（异常名 / 引号里的成语 / 错误码）会被 4 个字母序更靠前的
w=2 文件名挤出去。真库 #19 实测：

    旧键 layout_stress|layout_stress.py|setstylesheet|test_contrast   ← 全是不配当身份的文件名
    新键 深底浅字错配|页面是否真的刷了底|layout_stress|layout_stress.py

两个后果都实：① 同一条报错的键不含它的招牌，下次换个文件名组合就漂（欠账永不收编）；
② 只含通用文件名的键会把**不同**报错并成一个（凡是提到 layout_stress.py 的失败同键），
把 A 的修法安到 B 上——比空转更坏。

存量必须一起处理：`errors.sig` 是登记时算好存下来的，`_error_covered` 第一道走「键逐字
相等」（R834 的承诺：登记一次、下次同签名秒命中）。尺子换了不给旧行换键，等于把这 27 条
已登记的修法全部降级成模糊匹配。口径照 R926：
  · provenance 用**登记时的输入形状**复算：`error_sig_of(detail) or error_sig_of(title)`
    （brain.py:12326）；复算出的旧键 ≠ 存量 sig → 判不了，原样不动；
  · 旧键==存量 sig 且新键!=旧键 → 才是漂移；新键==旧键的行一个字都不写；
  · 旧键原样落 `pre_sig`，可复核、可回滚；默认 dry_run；幂等。
"""
import brain

# 真库 #19 的形状：一个 w=3 招牌 + 一堆字母序更靠前的 w=2 文件名
PROSE = "深色主题下四个页面不画底色，内容不可见"
SIG_TEXT = ("KeyError: '深底浅字错配' 检查 layout_stress setstylesheet test_contrast "
            "window.py 四个页面刷底")


def _w(terms):
    return dict(terms)


# ── 键本身 ────────────────────────────────────────────────────────────────
def test_high_weight_anchor_survives_the_four_slots():
    terms = brain.error_signature_terms(SIG_TEXT)
    assert _w(terms).get("深底浅字错配") == 3, terms
    key = brain._error_sig_key(terms)
    assert "深底浅字错配" in key, key
    assert "keyerror" in key, key


def test_weight_order_beats_alphabetical_within_the_key():
    """键的第 1 段必须是招牌（w=3），不是碰巧字母序靠前的文件名。"""
    terms = brain.error_signature_terms(SIG_TEXT)
    parts = brain._error_sig_key(terms).split("|")
    weights = _w(terms)
    assert weights.get(parts[0]) == 3, parts
    # 所有装得下的 w=3 都要在键里，不许被 w=2 顶掉
    for t, w in terms:
        if w >= 3 and sum(1 for x in terms if x[1] >= 3) <= 4:
            assert t in parts, (t, parts)


def test_key_is_still_order_independent_and_bounded():
    terms = brain.error_signature_terms(SIG_TEXT)
    assert brain._error_sig_key(terms) == brain._error_sig_key(list(reversed(terms)))
    assert len(brain._error_sig_key(terms).split("|")) <= 4


def test_legacy_ruler_still_reproduces_the_old_shape():
    """留一把冻住的旧尺子不是装饰：存量重签的 provenance 判据要靠它复算。

    旧尺子的定义就是「截 6 个 + 按字母取 4」，改一个字符都会让「这条存量到底是不是
    旧尺子刻的」判错——那才是真正会改写历史的地方。
    """
    terms = [("aaa", 2), ("mmm", 2), ("zzz", 2), ("招牌", 3), ("bbb", 2)]
    assert brain._error_sig_key_legacy(terms) == "aaa|bbb|mmm|zzz"
    assert brain._error_sig_key(terms) == "招牌|aaa|bbb|mmm"


# ── 存量重签 ──────────────────────────────────────────────────────────────
def _add_error(root, title, detail, project="p"):
    """登记一行并拿回 id——`error_add` 默认返回空串，只有 `return_info=True` 才给 id
    （R927 已经踩过一次：拿返回当 id，UPDATE 打在空气上，样本静默失效）。
    """
    info = brain.error_add(root, "qoder", title, detail=detail, project=project,
                           return_info=True)
    return int(info["id"])


def _seed_old_ruler_row(root, title, detail):
    """按**旧尺子**登记一行（模拟 R929 之前落库的存量）。

    复算走 `brain._error_sig_terms`——与登记通道同一个输入形状；在测试里自己拼
    `title + detail` 就会造出一批「复现不了」的假样本（本轮取证踩过）。
    """
    eid = _add_error(root, title, detail)
    old = brain._error_sig_key_legacy(brain._error_sig_terms(detail, title))
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE errors SET sig=? WHERE id=?", (old, eid))
    return eid, old


def test_drifted_row_is_resigned_and_old_key_kept(hub_root):
    brain.init_db(hub_root)
    eid, old = _seed_old_ruler_row(hub_root, PROSE, SIG_TEXT)
    got = brain.resign_error_sigs(hub_root, apply=True)
    assert got["drifted"] >= 1 and got["resigned"] >= 1, got
    with brain.db_conn(hub_root) as conn:
        r = conn.execute("SELECT sig, COALESCE(pre_sig,'') AS pre_sig FROM errors"
                         " WHERE id=?", (eid,)).fetchone()
    assert r["pre_sig"] == old, "旧键要原样留证"
    assert "深底浅字错配" in r["sig"], r


def test_resign_restores_the_exact_match_path(hub_root):
    """这轮的全部收益点：`errors.sig` 与召回侧算出的键**逐字相等**才有的「秒命中」。

    只测「新键长得好」不够——R834 的承诺是 `SELECT 1 FROM errors WHERE sig=?` 这一枪
    打得到。故意不靠 `_error_covered` 的模糊兜底当证据：那条路会把登记行自己的正文
    也算成「有答案」，测不出签名键到底通不通。
    """
    brain.init_db(hub_root)
    eid, old = _seed_old_ruler_row(hub_root, PROSE, SIG_TEXT)
    terms = brain.error_signature_terms(brain._error_voice(
        brain._diagnosis_span(brain._error_span(SIG_TEXT))))
    key = brain._error_sig_key(terms)

    def hit():
        with brain.db_conn(hub_root) as conn:
            return conn.execute("SELECT 1 FROM errors WHERE sig=? AND"
                                " (COALESCE(detail,'')<>'' OR COALESCE(undo,'')<>'')"
                                " LIMIT 1", (key,)).fetchone()

    assert hit() is None, "换了尺子，旧行本就不该被新键命中（这就是本轮要修的现实）"
    brain.resign_error_sigs(hub_root, apply=True)
    assert hit() is not None, "重签后同一条报错要按键逐字命中"
    with brain.db_conn(hub_root) as conn:
        now = conn.execute("SELECT sig FROM errors WHERE id=?", (eid,)).fetchone()["sig"]
    assert now == key == brain.error_sig_of(SIG_TEXT), (now, key)


def test_unchanged_and_unreproducible_rows_are_not_written(hub_root):
    brain.init_db(hub_root)
    same_eid = _add_error(hub_root, "普通标题", SIG_TEXT)
    with brain.db_conn(hub_root) as conn:
        same_sig = conn.execute("SELECT sig FROM errors WHERE id=?", (same_eid,)).fetchone()["sig"]
    junk_eid, _ = _seed_old_ruler_row(hub_root, PROSE, SIG_TEXT)
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE errors SET sig=? WHERE id=?", ("这条根本不是尺子刻的", junk_eid))
    got = brain.resign_error_sigs(hub_root, apply=True)
    assert got["unreproducible"] == 1, got
    assert got["same"] >= 1, got
    with brain.db_conn(hub_root) as conn:
        rows = {r["id"]: r["sig"] for r in conn.execute("SELECT id, sig FROM errors")}
    assert rows[same_eid] == same_sig, "新尺子算出同一个键的行不该被写"
    assert rows[junk_eid] == "这条根本不是尺子刻的", "复现不了存量 sig 的行一律不动"


def test_dry_run_writes_nothing_and_second_run_is_noop(hub_root):
    brain.init_db(hub_root)
    eid, old = _seed_old_ruler_row(hub_root, PROSE, SIG_TEXT)
    got = brain.resign_error_sigs(hub_root)         # 默认 dry_run
    assert got["drifted"] >= 1 and got["resigned"] == 0, got
    with brain.db_conn(hub_root) as conn:
        assert conn.execute("SELECT sig FROM errors WHERE id=?", (eid,)).fetchone()["sig"] == old
    brain.resign_error_sigs(hub_root, apply=True)
    second = brain.resign_error_sigs(hub_root, apply=True)
    assert second["resigned"] == 0 and second["drifted"] == 0, second


def test_legacy_db_without_the_column_is_migrated_not_crashed(hub_root):
    """老库没有 `pre_sig` 列（本轮才加的 ALTER）。判据不能靠调用顺序——线上实锤：
    生产库上直接跑重签报 `sqlite3.OperationalError: no such column: pre_sig`，
    因为管理入口未必先跑过 `init_db`。函数自己迁，迁完再写。
    """
    brain.init_db(hub_root)
    eid, old = _seed_old_ruler_row(hub_root, PROSE, SIG_TEXT)
    with brain.db_conn(hub_root) as conn:
        conn.execute("ALTER TABLE errors DROP COLUMN pre_sig")
    got = brain.resign_error_sigs(hub_root, apply=True)
    assert got["resigned"] >= 1, got
    with brain.db_conn(hub_root) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(errors)")}
        r = conn.execute("SELECT sig, pre_sig FROM errors WHERE id=?", (eid,)).fetchone()
    assert "pre_sig" in cols, cols
    assert r["pre_sig"] == old and r["sig"] == brain.error_sig_of(SIG_TEXT), dict(r)


def test_limit_caps_the_write(hub_root):
    brain.init_db(hub_root)
    for i in range(3):
        _seed_old_ruler_row(hub_root, f"{PROSE} {i}", SIG_TEXT + f" 第{i}次")
    got = brain.resign_error_sigs(hub_root, apply=True, limit=2)
    assert got["drifted"] == 3 and got["resigned"] == 2, got


def test_empty_sig_rows_are_counted_not_fabricated(hub_root):
    """更早的库有 sig="" 的行（R834 之前登记的）。补键是**新填**，不是重签——分开报数，
    否则「重签 N 行」里混着「本来没签名」的行，回滚时不知道该还原谁。
    """
    brain.init_db(hub_root)
    eid = _add_error(hub_root, PROSE, SIG_TEXT)
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE errors SET sig='' WHERE id=?", (eid,))
    got = brain.resign_error_sigs(hub_root, apply=True)
    assert got["no_sig"] == 1 and got["drifted"] == 0, got
    with brain.db_conn(hub_root) as conn:
        r = conn.execute("SELECT sig, pre_sig FROM errors WHERE id=?", (eid,)).fetchone()
    assert r["sig"] == brain.error_sig_of(SIG_TEXT), r
    assert r["pre_sig"] is None, "NULL 标记本轮首次补键，undo 才能与未触碰行区分"


def test_first_key_rows_can_be_undone(hub_root):
    import importlib.util
    from pathlib import Path

    brain.init_db(hub_root)
    eid = _add_error(hub_root, PROSE, SIG_TEXT)
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE errors SET sig='' WHERE id=?", (eid,))
    got = brain.resign_error_sigs(hub_root, apply=True)
    assert got["no_sig"] == 1 and got["resigned"] == 1, got

    path = Path(__file__).resolve().parents[2] / "tools" / "error_sig_resign_r929.py"
    spec = importlib.util.spec_from_file_location("error_sig_resign_r929", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.undo(hub_root)

    with brain.db_conn(hub_root) as conn:
        row = conn.execute("SELECT sig, pre_sig FROM errors WHERE id=?", (eid,)).fetchone()
    assert row["sig"] == "", "首次补键也要能还原成原来的空签名"
    assert row["pre_sig"] == "", "还原后要回到未触碰标记"
