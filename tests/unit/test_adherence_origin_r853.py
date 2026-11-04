# -*- coding: utf-8 -*-
"""R853：装置写的注入行不许进公共账本（origin 列）。

修之前会怎样失败（真库取证，不是假想的脏）：`recall_adherence` 181 行里 116 行是
tools/ 下的评测与探针写的——`eval-1…12`、`probe-r833`、`smoke-r837`、`test-r828`、
`e2e-adh-*`。这些会话**永远不会有对应的产出文本**（装置跑完就走），所以 116 行全部
落在「未观测」，把 R852 那套三态账压成一个假结论：
  · 账面采纳率 7.8%（分母 167 行掺了装置）→ 剔除后 17.5%（分母 63 行）；
  · 闭环点名把 `claude/test-r828`、`qoder/probe-r833` 这种装置会话当成「63h 没人判定」
    的生产欠账报出去——报出来的「问题」其实是自己的测试脚手架。

边界（越界就变成把尺子也锁在门外）：
  · 装置行**照样判定**、照样写 used/decided_at——R849 的实机探针靠读自己那几行的 used
    判 PASS，一刀不判等于废掉这把尺子；被拦的是**外溢**：`adopt_count` 只由生产行加。
  · 来历由**写入口声明**（`prompt_recall(origin=…)`），会话形状判别只用于那次历史回填；
    认不出的来历一律按装置处理（宁可少算一笔采纳，不可多算）。
  · `agent|session` 前缀是写入口老 bug 留下的历史行：回填不归一化就会漏 3 行，
    顶着 agent 名躲过所有形状匹配。
"""
import datetime
import sqlite3
from pathlib import Path

import brain
import ops

PROJECT = "装置隔离-测试-对象问题"

OLD_DDL = (
    "CREATE TABLE recall_adherence ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT '',"
    " session TEXT DEFAULT '', agent TEXT DEFAULT '', memory_id INTEGER,"
    " project TEXT DEFAULT '', strength TEXT DEFAULT 'strong', fp TEXT DEFAULT '',"
    " cited INTEGER DEFAULT 0, used INTEGER DEFAULT 0)"
)


def _now(offset_min: int = 0) -> str:
    return (datetime.datetime.now() - datetime.timedelta(minutes=offset_min)
            ).strftime("%Y-%m-%dT%H:%M:%S")


def _hub(hub_root: str) -> int:
    brain.init_db(hub_root)
    for a in ("qoder", "workbuddy"):
        brain.add_record(hub_root, PROJECT, a, _now()[:10], "开工", "对齐进度")
    return brain.add_memory(hub_root, "装置隔离测试记忆：kangaroo_r853.py 的 origin 账。",
                            kind="lesson", tags="r853", agent="qoder")


def _row(hub_root: str, mid: int, session: str, origin: str = "live", ts: str = "",
         fp: str = '["kangaroo_r853.py"]', used: int = 0, decided: bool = False,
         agent: str = "qoder") -> None:
    with brain.db_conn(hub_root) as conn:
        conn.execute(
            "INSERT INTO recall_adherence(ts,session,agent,memory_id,project,strength,fp,"
            "used,decided_at,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts or _now(), session, agent, mid, PROJECT, "strong", fp, used,
             _now() if decided else "", origin))


def _capture(hub_root: str, sessions) -> None:
    for s in sessions:
        for _ in range(3):
            brain.journal_add(hub_root, "qoder", "brain_adherence", "answer",
                              note=f"checked=4 matched=1 mem=[7] chars=4200 "
                                   f"src=transcript sess={s[:16]}")


def _find(detail, key: str) -> str:
    return next(d for d in detail if key in d)


# —— 一、统计层：装置行不进分母，但要报「排除了多少」 ——
def test_device_rows_leave_the_denominator(hub_root):
    mid = _hub(hub_root)
    for _ in range(4):
        _row(hub_root, mid, "prod-1")
    _row(hub_root, mid, "prod-1", used=1, decided=True)
    for _ in range(20):
        _row(hub_root, mid, "eval-3", origin="eval")       # 装置：永远不会被判定
    _row(hub_root, mid, "probe-9", origin="probe", used=1, decided=True)
    st = brain.recall_adherence_stats(hub_root, days=7)
    assert st["injected"] == 5 and st["fingerprintable"] == 5, st
    assert st["used"] == 1 and st["rate"] == 20.0, st      # 掺装置的话会算成 1/26=3.8%
    assert st["device_rows"] == 21, st                     # 排除了多少本身要可核查


def test_empty_ledger_with_only_devices_is_honest(hub_root):
    """整张账都是装置写的：报「没有生产数据」，不报「采纳率 0%」。"""
    mid = _hub(hub_root)
    for _ in range(6):
        _row(hub_root, mid, "eval-1", origin="eval")
    st = brain.recall_adherence_stats(hub_root, days=7)
    assert st["injected"] == 0 and st["device_rows"] == 6, st
    assert st["rate"] == 0.0 and st["fingerprintable"] == 0, st


# —— 二、判定层：装置行照判，但不许给真实记忆记一次「被用上」 ——
def test_device_hit_is_judged_but_does_not_bump_adopt_count(hub_root):
    mid = _hub(hub_root)
    _row(hub_root, mid, "live-1")
    _row(hub_root, mid, "eval-7", origin="eval")
    _capture(hub_root, ["live-1"])
    out = brain.mark_recall_used(hub_root, "live-1", "改了 kangaroo_r853.py")
    assert out["matched"] == 1 and out["memory_ids"] == [mid], out
    with brain.db_conn(hub_root) as conn:
        assert conn.execute("SELECT adopt_count FROM memories WHERE id=?",
                            (mid,)).fetchone()[0] == 1
    out2 = brain.mark_recall_used(hub_root, "eval-7", "改了 kangaroo_r853.py")
    assert out2["matched"] == 1, out2                       # 尺子还在：判定照做
    assert out2["device_matched"] == 1 and out2["memory_ids"] == [], out2
    with brain.db_conn(hub_root) as conn:
        r = conn.execute("SELECT used, decided_at, origin FROM recall_adherence"
                         " WHERE session='eval-7'").fetchone()
        assert (r[0], r[2]) == (1, "eval") and r[1], r      # used 写了…
        assert conn.execute("SELECT adopt_count FROM memories WHERE id=?",
                            (mid,)).fetchone()[0] == 1, "…但 adopt_count 没涨（外溢被拦）"


# —— 三、来历由写入口声明 ——
def test_prompt_recall_stores_declared_origin(hub_root, tmp_path):
    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "打包体检跑 tools/kangaroo_r853_check.py，缺失项复现。",
                           kind="lesson", tags="打包,exe", agent="qoder")
    res = brain.prompt_recall(hub_root, "kangaroo_r853_check 怎么复现", agent="workbuddy",
                              session="eval-99", state_path=str(tmp_path / "e.json"),
                              update_state=False, origin="eval")
    assert mid in res["memory_ids"], res["text"]
    with brain.db_conn(hub_root) as conn:
        origins = [r[0] for r in conn.execute(
            "SELECT DISTINCT origin FROM recall_adherence WHERE session='eval-99'")]
    assert origins == ["eval"], origins


def test_unrecognized_origin_is_treated_as_device(hub_root):
    """认不出的来历按装置处理：宁可少算一笔采纳，不可让来历不明的行进生产分母。"""
    mid = _hub(hub_root)
    assert brain._record_adherence(hub_root, "typo-1", "qoder", [{"id": mid, "content":
        "kangaroo_r853.py 的 origin 账。", "tags": "r853"}], PROJECT, "strong",
        "kangaroo_r853.py", origin="LIVE-typo") == 1
    with brain.db_conn(hub_root) as conn:
        got = conn.execute("SELECT origin FROM recall_adherence WHERE session='typo-1'"
                           ).fetchone()[0]
    assert got == "probe", got
    assert brain.recall_adherence_stats(hub_root, days=7)["device_rows"] == 1


# —— 四、闭环点名跳过装置会话 ——
def test_closure_naming_skips_device_sessions(hub_root):
    mid = _hub(hub_root)
    old = _now(offset_min=60 * 24 * 2)                       # 两天前，远超闲置线
    _row(hub_root, mid, "test-r828", origin="probe", agent="claude", ts=old)
    _row(hub_root, mid, "probe-r833", origin="probe", ts=old)
    _row(hub_root, mid, "prod-idle", ts=old)                 # 真欠账，要被点名
    _row(hub_root, mid, "prod-run")                          # 判过的生产会话
    _capture(hub_root, ["prod-run"])
    # R875：捕获起点必须早于 prod-idle（两天前），否则会被当成「历史断档」豁免——
    # 本用例要验的是装置会话跳过，不是历史分流。
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE journal SET ts=? WHERE action='brain_adherence'",
                     (_now(offset_min=60 * 24 * 3),))
    r = ops._adherence_health(hub_root)
    line = _find(r["detail"], "闭环账")
    assert "窗口内 2 个生产会话有注入" in line, line
    assert "test-r828" not in line and "probe-r833" not in line, line
    assert "prod-idle" in line, line                          # 生产欠账照常点
    assert "装置会话不参与点名" in line, line


def test_device_rows_are_reported_in_detail(hub_root):
    mid = _hub(hub_root)
    for _ in range(30):
        _row(hub_root, mid, "eval-1", origin="eval")
    for _ in range(4):
        _row(hub_root, mid, "prod-1")
    _row(hub_root, mid, "prod-1", used=1, decided=True)
    _capture(hub_root, ["prod-1"])
    r = ops._adherence_health(hub_root)
    assert "另有 30 笔注入是 eval/probe 写的" in _find(r["detail"], "装置隔离"), r["detail"]
    assert "注入 5 笔" in _find(r["detail"], "结果账"), r["detail"]


# —— 五、老库迁移：缺列要补、历史装置行要回填、生产行不许误伤 ——
def _make_old_db(hub_root: str, rows) -> None:
    db = Path(hub_root) / "_hub" / "brain.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(OLD_DDL)
        conn.executemany("INSERT INTO recall_adherence(ts,session,agent,memory_id,fp,used)"
                         " VALUES(?,?,?,?,?,?)", rows)


def test_migration_adds_columns_and_backfills_devices(hub_root):
    old = _now(offset_min=30)
    rows = [(old, "eval-1", "workbuddy", 1, '["a"]', 0),
            (old, "probe-r833", "qoder", 1, '["b"]', 0),
            (old, "smoke-r837", "workbuddy", 1, '["c"]', 0),
            (old, "test-r828", "claude", 1, '["d"]', 0),
            (old, "e2e-adh-3", "workbuddy", 1, '["e"]', 0),
            # 写入口老 bug 留下的 `agent|session`：不归一化就顶着 agent 名躲过匹配
            (old, "workbuddy|e2e-adh-2", "workbuddy", 1, '["f"]', 0),
            # 生产形状：宿主给的是 UUID / session_<hex>，一行都不许误标
            (old, "482cf18a-66a5-43fa-b22b-d263660def7c", "qoder", 1, '["g"]', 0),
            (old, "session_4f63eadf4cd5a1a5b599e05bc951ee5c", "zcode", 1, '["h"]', 0)]
    _make_old_db(hub_root, rows)
    brain.init_db(hub_root)                                  # 部署时本来就会跑这一步
    with brain.db_conn(hub_root) as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(recall_adherence)")]
        assert {"origin", "decided_at"} <= set(cols), cols    # decided_at 也一并兜底
        got = dict(conn.execute("SELECT session, origin FROM recall_adherence").fetchall())
    for s in ("eval-1", "probe-r833", "smoke-r837", "test-r828", "e2e-adh-3",
              "workbuddy|e2e-adh-2"):
        assert got[s] == "probe", (s, got[s])
    for s in ("482cf18a-66a5-43fa-b22b-d263660def7c", "session_4f63eadf4cd5a1a5b599e05bc951ee5c"):
        assert got[s] == "live", (s, got[s])


def test_migration_backfill_runs_only_once(hub_root):
    """回填是「缺列时跑一次」：第二次 init_db 不许把新写的 live 行改判成装置。"""
    _make_old_db(hub_root, [(_now(offset_min=30), "eval-1", "workbuddy", 1, '["a"]', 0)])
    brain.init_db(hub_root)
    _row(hub_root, 1, "eval-2", origin="live")               # 迁移后又写的同名形状
    brain.init_db(hub_root)
    with brain.db_conn(hub_root) as conn:
        got = dict(conn.execute("SELECT session, origin FROM recall_adherence").fetchall())
    assert got == {"eval-1": "probe", "eval-2": "live"}, got


def test_caliber_anchors_are_recorded(hub_root):
    """口径变更要留时间锚点：跨变更点的比率没有锚点就无从解释（R834 教训）。"""
    brain.init_db(hub_root)
    with brain.db_conn(hub_root) as conn:
        keys = {r[0] for r in conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'caliber:%'").fetchall()}
    assert {"caliber:adherence_three_state", "caliber:adherence_origin"} <= keys, keys
