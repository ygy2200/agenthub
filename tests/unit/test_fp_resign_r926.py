# -*- coding: utf-8 -*-
r"""R926：净化只管新行——历史账里「判不死」的指纹要保守重签。

修之前会怎样失败（2026-11-06 真库取证，`tools/fp_hygiene_diff_r925.py`）：R925 改的是
`recall_fingerprints` 的切法，而 `recall_adherence.fp` 是**落库时算好的**——
`mark_recall_used` 只拿存下来的串做 substring。于是已存在的 18 条记忆、25 个带反引号/
中文标点的 token 在 30 天窗口里仍然永远命中不了：尺子修好了，旧账上还刻着坏尺子，
回填通道再怎么跑也判不动它们。

口径（自由度只有这么点，越界就是改写历史证据）：
  · 只动 `used=0` 的行——已经领过功的证据不重写；
  · 重签条件：把旧 token 按 R925 的切法截干净后，**必须每个都还在**用当前记忆正文新抽出的
    指纹集合里（子集判据）。记忆正文被改过 → 不满足 → 跳过，绝不替它猜当年注入了什么；
  · 旧值原样留在 `pre_fp` 列，判据可复核、可回滚；
  · 默认 dry_run 只报数不写库；幂等：跑第二遍 resigned=0；
  · 窗口与采纳账保留窗口（ADHERENCE_KEEP_DAYS）同口径。
"""
import json

import brain

SKILL_MD = "c:\\users\\y\\.agents\\skills\\flclash-proxy\\skill.md"
CONTENT = f"凡动 FlClash 一律走 `{SKILL_MD}`（不准裸命令行）"
# 只有一个指纹的例子：真库 #476 的形状。用它测「解锁判定」，因为 `skill.md`、`flclash`
# 这类词本身就在路径里，脏不脏都能命中，拿来当证据等于没测。
LEGI = "e:\\legionzone"
CONTENT_LEGI = f"健康检查脚本在 {LEGI}；确认健康时才写心跳"


def _legacy_fps(text: str) -> list:
    r"""复原 R925 **之前**那把尺子：同一套 `_FP_PATTERNS`，只把切法换回旧的 `rstrip`。

    为什么测试要自己造脏值而不是直接调 `recall_fingerprints`：R925 已经把抽取器修好了，
    新写的行天生干净（第一版用例就栽在这儿——candidates=0，等于什么都没测）。
    生产库里那 18 条脏行是旧尺子刻的，只有按旧切法落库，才是「重签历史行」的真实现场。
    """
    out, seen, total = [], set(), 0
    for pat in brain._FP_PATTERNS:
        for m in pat.finditer(text):
            tok = m.group(0).strip().rstrip(".,;:)）】」").lower()
            if len(tok) < 4 or tok in seen or tok in brain._FP_STOP:
                continue
            seen.add(tok)
            if total + len(tok) > 160:
                continue
            out.append(tok)
            total += len(tok)
            if len(out) >= 6:
                break
    return out


def _seed(root: str, session: str = "s1", used: int = 0,
          content: str = CONTENT) -> int:
    mid = brain.add_memory(root, content, kind="lesson", tags="机制说明", agent="qoder")
    brain._record_adherence(root, session, "qoder",
                            [{"id": mid, "kind": "lesson", "content": content,
                              "tags": "机制说明"}],
                            project="", strength="strong", prompt="",
                            pool_texts=[], origin="live")
    legacy = _legacy_fps(content + " 机制说明")
    assert legacy != brain.recall_fingerprints(content + " 机制说明"), \
        "样本必须新旧切法不一致，否则造不出脏行"
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE recall_adherence SET fp=? WHERE session=?",
                     (json.dumps(legacy, ensure_ascii=False), session))
    if used:
        brain.mark_recall_used(root, session, content.replace("（", " "))
    return mid


def _row(root: str, session: str = "s1") -> dict:
    with brain.db_conn(root) as conn:
        r = conn.execute("SELECT id, fp, COALESCE(pre_fp,'') AS pre_fp, used"
                         " FROM recall_adherence WHERE session=?", (session,)).fetchone()
    return dict(r) if r else {}


def test_dirty_row_is_resigned_and_old_value_kept(hub_root):
    brain.init_db(hub_root)
    _seed(hub_root)
    before = _row(hub_root)
    assert any("`" in t for t in json.loads(before["fp"])), \
        f"样本本身要带脏 token：{before['fp']}"

    got = brain.refingerprint_rows(hub_root, days=30, apply=True)
    assert got["resigned"] == 1, got

    after = _row(hub_root)
    new = json.loads(after["fp"])
    assert not any("`" in t or "（" in t for t in new), new
    assert SKILL_MD.lower() in new, new
    assert json.loads(after["pre_fp"]) == json.loads(before["fp"]), "旧值要原样留证"


def test_resign_unlocks_the_judgement(hub_root):
    """这轮的全部收益点：重签之后，同一段产出文本才真的能判成「用上」。"""
    brain.init_db(hub_root)
    mid = _seed(hub_root, content=CONTENT_LEGI)
    evidence = f"我把心跳脚本挪到 {LEGI} 里跑了一遍"
    assert brain.mark_recall_used(hub_root, "s1", evidence)["matched"] == 0, \
        "脏指纹本就不该命中（这就是 R925 修之前的现场）"
    got = brain.refingerprint_rows(hub_root, days=30, apply=True)
    assert got["resigned"] == 1, got
    assert brain.mark_recall_used(hub_root, "s1", evidence)["matched"] == 1, "重签后要能判定"
    with brain.db_conn(hub_root) as conn:
        adopt = conn.execute("SELECT adopt_count FROM memories WHERE id=?",
                             (mid,)).fetchone()[0]
    assert adopt == 1, adopt


def test_dry_run_writes_nothing(hub_root):
    brain.init_db(hub_root)
    _seed(hub_root)
    before = _row(hub_root)
    got = brain.refingerprint_rows(hub_root, days=30)      # 默认只报数
    assert got["candidates"] == 1 and got["resigned"] == 0, got
    assert _row(hub_root) == before


def test_edited_memory_is_skipped(hub_root):
    """正文改过就跳过：不能替当年那次注入猜指纹。"""
    brain.init_db(hub_root)
    mid = _seed(hub_root)
    before = _row(hub_root)["fp"]
    brain.edit_memory(hub_root, mid, content="这条重写过了，跟原来那句毫无重合")
    got = brain.refingerprint_rows(hub_root, days=30, apply=True)
    assert got["resigned"] == 0 and got["skipped"] == 1, got
    assert _row(hub_root)["fp"] == before


def test_used_rows_are_never_touched(hub_root):
    """已经领过功的证据不重写（哪怕它的指纹是脏的）。"""
    brain.init_db(hub_root)
    _seed(hub_root, session="s-ok", used=1, content=CONTENT_LEGI)
    before = _row(hub_root, "s-ok")
    assert before["used"] == 1 and any("；" in t for t in json.loads(before["fp"])), before
    got = brain.refingerprint_rows(hub_root, days=30, apply=True)
    assert got["resigned"] == 0 and got["candidates"] == 0, \
        f"used=1 的行根本不该进候选：{got}"
    assert _row(hub_root, "s-ok") == before


def test_clean_rows_and_second_run_are_noops(hub_root):
    """幂等 + 不空转：干净行不进候选，跑第二遍 resigned=0。"""
    brain.init_db(hub_root)
    clean_mid = brain.add_memory(hub_root, "打包用 build.bat，脚本 agenthub_mcp.py",
                                 kind="lesson", tags="", agent="qoder")
    clean = brain.recall_fingerprints("打包用 build.bat，脚本 agenthub_mcp.py",
                                      pool_texts=[], prompt="")
    with brain.db_conn(hub_root) as conn:
        conn.execute("INSERT INTO recall_adherence(ts,session,agent,memory_id,project,"
                     "strength,fp,origin,directive) VALUES(?,?,?,?,?,?,?,?,?)",
                     (brain._now(), "s-clean", "qoder", clean_mid, "", "strong",
                      json.dumps(clean), "live", "unknown"))
    _seed(hub_root)
    first = brain.refingerprint_rows(hub_root, days=30, apply=True)
    assert first["resigned"] == 1, first
    assert first["candidates"] == 1, f"干净行不该进候选：{first}"
    second = brain.refingerprint_rows(hub_root, days=30, apply=True)
    assert second["resigned"] == 0, second


def test_out_of_window_rows_are_ignored(hub_root):
    """超出保留窗口的行本来就会被清掉，不动它。"""
    brain.init_db(hub_root)
    _seed(hub_root, session="s-old")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE recall_adherence SET ts=? WHERE session='s-old'",
                     ("2020-01-01T00:00:00",))
    got = brain.refingerprint_rows(hub_root, days=30, apply=True)
    assert got["resigned"] == 0 and got["candidates"] == 0, got


def test_unparsable_fp_is_skipped_not_crashed(hub_root):
    """账本里可能有非 JSON 的旧值（更早的版本），跳过而不是炸——判据不能靠运气。"""
    brain.init_db(hub_root)
    _seed(hub_root, session="s-junk")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE recall_adherence SET fp='不是JSON' WHERE session='s-junk'")
    got = brain.refingerprint_rows(hub_root, days=30, apply=True)
    assert got["resigned"] == 0 and got["skipped"] == 1, got
