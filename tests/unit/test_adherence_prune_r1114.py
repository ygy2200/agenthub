# -*- coding: utf-8 -*-
"""R1114：装置行物理治理 + 统计查询下推（兑现「表要有界」契约）。

为什么值得钉：brain.py 建表注释写着「表要**有界**：30 天保留、行数封顶」，
但 R1113 真库实测 device_rows=18385，是生产 510 笔的 36 倍。原因是
`_record_adherence` 的清理只在写入那一刻触发，30 天内装置行照样堆着；
`recall_adherence_stats` 又把全部行捞进 Python 再过滤，纯浪费。

外部依据（R1114 取证）：
  · n4n.ai《How to evaluate AI agent memory systems》：
    「Never test against production data」「Isolate the memory backend」
  · bigyan.dev《The Eval Set Contamination Problem》：
    「Enforce a hard train/eval firewall with provenance tags … belongs in code
     as a data contract enforced at the seams of the pipeline」
  · egoistai《LLM Evaluation Data Contamination: Keep Your Benchmarks Honest》：
    「Keep a registry for each item: creator, source, creation date, intended split」
  · Applied Compute《Memory in the wild》：按 >15 次调用的 bucket 才出指标（小样本不判）

本文件先钉五件事：命令注册 / 默认预演零删除 / --apply 只删超龄装置行 /
生产行与未知来历一律不动 / 统计下推后 SQL 只捞生产行。
"""
import contextlib
import datetime
import io
import pathlib

import brain
import cli


def _seed(root, rows):
    """rows: (age_days, origin, fp, used)。origin=None 落 NULL（历史行形状）。"""
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)  # init_db 不建目录（同 R1035）
    brain.init_db(root)
    now = datetime.datetime.now()
    with brain.db_conn(root) as conn:
        for i, (age, origin, fp, used) in enumerate(rows):
            ts = (now - datetime.timedelta(days=age)).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                "INSERT INTO recall_adherence(ts,session,agent,memory_id,project,strength,"
                " fp,used,decided_at,origin) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, "s%d" % i, "workbuddy", 1, "p", "strong", fp, used, "", origin))


def _all(root):
    with brain.db_conn(root) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT ts,origin,session,used FROM recall_adherence ORDER BY id")]


def test_command_registered():
    src = (pathlib.Path(__file__).resolve().parents[2] / "cli.py").read_text(encoding="utf-8")
    assert '"adherence-prune"' in src, "子命令没注册"
    assert "def cmd_adherence_prune" in src


def test_dry_run_never_deletes(tmp_path):
    root = str(tmp_path / "hub")
    _seed(root, [(40, "eval", '["token"]', 0), (40, "probe", "[]", 0),
                 (40, "live", '["token"]', 1), (1, "eval", "[]", 0)])
    before = _all(root)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.cmd_adherence_prune(root, apply=False, days=7)
    assert rc == 0, buf.getvalue()[:300]
    assert "预演" in buf.getvalue()
    assert _all(root) == before, "预演碰了库"


def test_apply_only_old_device_rows(tmp_path):
    root = str(tmp_path / "hub")
    _seed(root, [(40, "eval", '["token"]', 0),      # 超龄装置 -> 删
                 (40, "probe", "[]", 0),            # 超龄装置 -> 删
                 (40, "EVAL", "[]", 0),             # 大小写变体 -> 删
                 (1, "eval", "[]", 0),              # 保留窗口内 -> 留
                 (40, "live", '["token"]', 1),      # 生产行再老也不删
                 (40, "brief", "[]", 0),            # 简报生产行不删
                 (40, None, "[]", 0),               # 历史 NULL 按生产处理 -> 不删
                 (40, "weird", "[]", 0)])           # 未知来历不猜 -> 不删
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.cmd_adherence_prune(root, apply=True, days=7)
    assert rc == 0, buf.getvalue()[:300]
    left = _all(root)
    assert len(left) == 5, [r["origin"] for r in left]
    assert {r["origin"] for r in left} == {"eval", "live", "brief", None, "weird"}
    assert "已删" in buf.getvalue() and "3" in buf.getvalue()


def test_prune_report_counts(tmp_path):
    root = str(tmp_path / "hub")
    _seed(root, [(40, "eval", "[]", 0), (1, "eval", "[]", 0), (40, "live", "[]", 0)])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_adherence_prune(root, apply=False, days=7)
    out = buf.getvalue()
    assert "装置" in out and "生产" in out, out


def test_stats_scanned_rows_is_production_only(tmp_path):
    """下推前：SQL 捞 103 行进 Python 再过滤；下推后 scanned_rows 必须 = 生产 3 行。"""
    root = str(tmp_path / "hub")
    rows = [(1, "eval", "[]", 0) for _ in range(100)]
    rows += [(1, "live", '["token"]', 1) for _ in range(3)]
    _seed(root, rows)
    st = brain.recall_adherence_stats(root, days=30)
    assert st["device_rows"] == 100, st
    assert st["injected"] == 3, st
    assert st["scanned_rows"] == 3, "SQL 没下推：装置行仍被捞进 Python"
