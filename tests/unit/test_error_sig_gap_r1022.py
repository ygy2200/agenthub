# -*- coding: utf-8 -*-
r"""R1022：登记了却没签名键——「登记一次下次秒命中」在这一行上不成立。

真库实锤（2026-11-08 19:51）：用**旧代码的 MCP 进程**登记了错误 #33
（WebFetch/抓 GitHub 原文失败）。行看起来一切正常——标题、现象、回滚都在——
只有 errors.sig 是空的。后果不是「报错」而是「退化」：召回只能靠模糊 LIKE 命中，
逐字相同那条最稳的路走不通，而且**没有任何地方会告诉你这件事**。

这是「部署≠生效」在写入口上的变体：旧进程不只读旧代码，还会**静默丢掉新字段**。

本文件锁：
1. error_sig_gap 只把「sig 为空但按同一把尺子算得出键」的行算缺口
   ——本来就没有签名形状的老行是正常行，不算缺口；
2. ops 的 error_debt 摘要必须报出缺口并点名成因（写入方是旧进程）；
3. 回填后按 sig 逐字命中（精确路），不是只能靠模糊 LIKE；
4. 没有缺口时不报（不许制造噪声）。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402
import ops  # noqa: E402


@pytest.fixture
def root_with_error(tmp_path):
    root = str(tmp_path)
    brain.init_db(root)
    brain.error_add(root, "workbuddy", "WebFetch 抓 GitHub 原文失败",
                    'Error during web fetch for "https://raw.githubusercontent.com/x/y.py": Request failed',
                    "P", "走代理重试")
    return root


def test_sig_written_on_normal_registration(root_with_error):
    with brain.db_conn(root_with_error) as conn:
        conn.row_factory = brain.sqlite3.Row
        row = conn.execute("SELECT sig FROM errors").fetchone()
    assert (row["sig"] or "").startswith("error during web fetch")


def test_sig_gap_detects_empty_sig(tmp_path):
    root = str(tmp_path)
    brain.init_db(root)
    brain.error_add(root, "workbuddy", "某条错",
                    'Error during web fetch for "https://x/y": failed', "P", "重试")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE errors SET sig='' WHERE id=1")
    gap = brain.error_sig_gap(root)
    assert gap["total"] == 1
    assert gap["gap"] == 1, "算得出键的空 sig 行必须算缺口"
    assert gap["rows"][0]["sig"].startswith("error during web fetch")


def test_sig_gap_ignores_rows_without_signature(tmp_path):
    """没有签名形状的老行是正常行，不算缺口（否则噪声）。"""
    root = str(tmp_path)
    brain.init_db(root)
    brain.error_add(root, "workbuddy", "双会话撞车", "另一 agent 正在改同一批文件", "P", "停手")
    gap = brain.error_sig_gap(root)
    assert gap["gap"] == 0


def test_ops_error_debt_reports_gap(tmp_path):
    root = str(tmp_path)
    brain.init_db(root)
    brain.error_add(root, "workbuddy", "某条错",
                    'Error during web fetch for "https://x/y": failed', "P", "重试")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE errors SET sig='' WHERE id=1")
    r = ops.run_check("error_debt", root=root)
    assert "缺签名键" in r["summary"], r["summary"]


def test_exact_sig_match_after_backfill(tmp_path):
    """回填后走的是逐字相同那条最稳的路（不是只能靠模糊 LIKE）。"""
    root = str(tmp_path)
    brain.init_db(root)
    brain.error_add(root, "workbuddy", "WebFetch 抓 GitHub 原文失败",
                    'Error during web fetch for "https://raw.githubusercontent.com/x/y.py": failed',
                    "P", "走代理重试")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE errors SET sig='' WHERE id=1")
        conn.execute("UPDATE errors SET sig=? WHERE id=1",
                     (brain.error_sig_of('Error during web fetch for "https://raw.githubusercontent.com/x/y.py": failed'),))
    r = brain.error_recall(root, 'Error during web fetch for "https://other/z.py": failed',
                           tool="WebFetch", agent="workbuddy", cwd=root, count=False, session="r1022-test")
    assert r["error_ids"], "回填后应命中已登记的修法"
    assert "走代理" in (r["text"] or "")


# ---- R1023：有签名率体温计 ----

def _log_attempts(root, shaped=2, unshaped=3):
    for i in range(shaped):
        brain.log_search(root, "error_recall", "syntaxerror", 0, agent="workbuddy")
    for i in range(unshaped):
        brain.log_search(root, "error_recall_miss", "(no_signature)", 0, agent="workbuddy")


def test_ops_error_debt_reports_signature_rate(tmp_path):
    """「抽得出签名」的比例要摆出来——它是部署≠生效的体温计。"""
    root = str(tmp_path)
    brain.init_db(root)
    _log_attempts(root, shaped=2, unshaped=3)
    r = ops.run_check("error_debt", root=root)
    assert "有签名率" in r["summary"], r["summary"]


def test_signature_rate_denominator_is_all_attempts(tmp_path):
    root = str(tmp_path)
    brain.init_db(root)
    _log_attempts(root, shaped=2, unshaped=3)
    debt = brain.error_debt(root, days=7)
    # 三次无签名触发进了欠账分母；有签名的那两次属另一路（命中/答不上由 error_recall 记）
    assert debt["asked"] == 3
    assert debt["miss_unshaped"] == 3
    assert debt["asked_shaped"] <= debt["asked"]


# ---- R1023：有签名率体温计 ----

def _log_attempts(root, shaped=2, unshaped=3):
    for i in range(shaped):
        brain.log_search(root, "error_recall", "syntaxerror", 0, agent="workbuddy")
    for i in range(unshaped):
        brain.log_search(root, "error_recall_miss", "(no_signature)", 0, agent="workbuddy")


def test_ops_error_debt_reports_signature_rate(tmp_path):
    """「抽得出签名」的比例要摆出来——它是部署≠生效的体温计。"""
    root = str(tmp_path)
    brain.init_db(root)
    _log_attempts(root, shaped=2, unshaped=3)
    r = ops.run_check("error_debt", root=root)
    assert "有签名率" in r["summary"], r["summary"]


def test_signature_rate_denominator_is_all_attempts(tmp_path):
    root = str(tmp_path)
    brain.init_db(root)
    _log_attempts(root, shaped=2, unshaped=3)
    debt = brain.error_debt(root, days=7)
    # 三次无签名触发进了欠账分母；有签名的那两次属另一路（命中/答不上由 error_recall 记）
    assert debt["asked"] == 3
    assert debt["miss_unshaped"] == 3
    assert debt["asked_shaped"] <= debt["asked"]
