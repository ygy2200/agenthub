# -*- coding: utf-8 -*-
r"""R1016：失败样本存「错误本体」而不是「命令回声」——采集面本身要能被读懂。

真库实锤（2026-11-08，fail_events 153 枚）：
载荷是三段——①「失败调用：<命令>」②宿主回显的「Command: <命令>」③真正的报错正文。
旧实现截前 400 字，存下来的几乎全是①②，③被整段切掉：
  head = '失败调用：cd "D:/…" && git tag -a v2.41.260 … \\nfatal: unable to access …'
  → 400 字预算被命令回声吃掉，真正的错误正文在后半段，读数时看到的是「一条 git 命令」。
所以「无签名触发说不清是什么」一直说不清——样本存的不是错误。

本文件锁：
1. _fail_head 摘掉命令回声行，保留错误正文；
2. 挑不出诊断行时退回「去掉回声行」的正文，而不是原样返回命令；
3. sample_failure 落库的 head 是错误本体（落库链路真正验证一次）；
4. fail_summary 给出「样本可归因率」，并按形状拆。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402

PAYLOAD = (
    '失败调用：cd "D:/agent资料/zcode项目记录/AgentHub-多agent文件统一管理/AgentHub" '
    '&& git tag -a v2.41.260 -m "v2.41.260: R1014"\n'
    'Command: cd "D:/agent资料/zcode项目记录/AgentHub-多agent文件统一管理/AgentHub" '
    '&& git tag -a v2.41.260 -m "v2.41.260: R1014"\n'
    "fatal: unable to access 'https://github.com/ygy2200/agenthub.git/': "
    "schannel: failed to receive handshake, SSL/TLS connection failed\n"
)


def test_fail_head_drops_command_echo():
    head = brain._fail_head(PAYLOAD)
    assert "失败调用：" not in head
    assert "Command:" not in head
    assert "fatal: unable to access" in head
    assert "schannel" in head


def test_fail_head_keeps_error_when_no_shape_match():
    txt = "失败调用：python -m pytest\nCommand: python -m pytest\n没有形状但仍是正文"
    head = brain._fail_head(txt)
    assert "失败调用：" not in head
    assert "没有形状但仍是正文" in head


def test_fail_head_never_empty():
    assert brain._fail_head("") == ""
    # 只有命令回声、没有错误正文时：退回原载荷（不能凭空造空样本，也不能把内容丢了）
    only = brain._fail_head("失败调用：only-echo")
    assert only and "only-echo" in only


def test_sample_failure_stores_error_body(tmp_path):
    root = str(tmp_path)
    brain.init_db(root)
    brain.sample_failure(root, agent="qoder", tool="Bash", event="PostToolUse",
                         payload=PAYLOAD)
    with brain.db_conn(root) as conn:
        conn.row_factory = brain.sqlite3.Row
        row = conn.execute("SELECT head, shape, chars FROM fail_events").fetchone()
    assert "fatal: unable to access" in row["head"]
    assert "Command:" not in row["head"]
    assert int(row["chars"]) > len(row["head"])   # chars 记原载荷长度，不等于存下来的正文


def test_fail_summary_reports_attributable_rate(tmp_path):
    root = str(tmp_path)
    brain.init_db(root)
    brain.sample_failure(root, agent="qoder", tool="Bash", event="PostToolUse",
                         payload=PAYLOAD)
    brain.sample_failure(root, agent="qoder", tool="Bash", event="PostToolUse",
                         payload="失败调用：ls\nCommand: ls\n只说有问题不指明对象")
    s = brain.fail_summary(root, days=7)
    assert s["sample_total"] == 2
    assert 0 <= s["attributable"] <= 2
    assert s["attributable_pct"] == round(100.0 * s["attributable"] / 2, 1)
    assert s["attributable_by_shape"], "按形状也要拆一份"
    # 每枚样本都要带 has_sig 标记，巡检/归因不用再自己抽一遍
    for e in s["examples"]:
        assert "has_sig" in e


def test_ops_failure_samples_shows_attributable(tmp_path):
    import ops
    root = str(tmp_path)
    brain.init_db(root)
    brain.sample_failure(root, agent="qoder", tool="Bash", event="PostToolUse",
                         payload=PAYLOAD)
    r = ops.run_check("failure_samples", root=root)
    assert r["ok"] is True
    assert "可归因" in r["summary"]
