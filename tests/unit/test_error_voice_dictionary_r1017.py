# -*- coding: utf-8 -*-
r"""R1017–R1018：失败成语词典按真库样本补齐 + 宿主工具层的失败口音认得出来。

真库实测（2026-11-08，fail_events 153 枚失败样本）：
  · 改前「抽得出可追签名」27/153 = 17.6%；「签名 ∧ 可上墙形状」17/153 = 11.1%。
    缺口最大的两类：
      ① 宿主工具层失败口音（web fetch / glob / 编辑器类工具）——老词典一条不收，
         且带 URL/引号被归成 content_only 挡在上墙之外（86 枚，最大头）；
      ② git/TLS 层（fatal: unable to access / failed to receive handshake）——同样是空。
  · 改后：可归因 103/153 = 67.3%，可上墙 88/153 = 57.5%。

纪律（R859/R865/B045/B066 的老规矩，逐条守住）：
  · 只收「失败本身」的短语，不收泛前缀（failed to / error / exit code）；
  · 宿主口音必须出现在**行首**才认——网页正文中间出现的同名词不算这次调用失败；
  · 新短语必须同时进锚点表，否则 `_err_accept` 把它当对象词退回去（等于白加）。

本文件锁：
1. 每条新短语都能从真样本抽出 w≥3 招牌；
2. 泛前缀仍然抽不出招牌（防词典变宽后什么都像签名）；
3. 宿主口音认形状、且只认行首；
4. 新短语是强锚点（一条即定身份）。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402

# 真库样本里出现过的失败正文（逐条来自 fail_events，不是想象的句子）
REAL_SAMPLES = [
    ('Error during web fetch for "https://raw.githubusercontent.com/x/y.py": Request failed',
     "error during web fetch"),
    ("glob error: Error during glob search operation: Search timeout after 30000 ms",
     "error during glob search operation"),
    ("fatal: unable to access 'https://github.com/ygy2200/agenthub.git/': "
     "schannel: failed to receive handshake, SSL/TLS connection failed",
     "failed to receive handshake"),
    ("fatal: not a git repository (or any of the parent directories): .git",
     "not a git repository"),
    ("File does not exist. Note: your current working directory is C:/Users/y",
     "file does not exist"),
    ("File has not been read yet: D:/agent资料/x.py",
     "file has not been read yet"),
    ("File content (34811 tokens) exceeds maximum allowed tokens (25000). Use offset.",
     "exceeds maximum allowed tokens"),
    ("ERROR: file or directory not found: tests/unit/test_x.py",
     "file or directory not found"),
    ("The process cannot access the file because it is being used by another process.",
     "being used by another process"),
    ("curl: (28) Operation timed out after 30000 milliseconds",
     "timed out"),
]


@pytest.mark.parametrize("text,expect", REAL_SAMPLES)
def test_real_failures_yield_signature(text, expect):
    terms = brain.error_signature_terms(brain._error_voice(brain._diagnosis_span(text)))
    high = [t for t, w in terms if w >= 3]
    assert high, f"这条真实失败抽不出招牌：{text[:60]}"
    assert any(expect in t for t in high), f"招牌里没有 {expect}：{high}"
    # 招牌必须是「独一份」的（能单独认出这条错），否则 _err_accept 会把它当对象词
    for t in high:
        if expect in t:
            assert brain._err_distinctive(t)


@pytest.mark.parametrize("text", [
    "failed to do something",
    "error",
    "Exit code 1",
    "Exit code 28",
    "some generic failure happened",
])
def test_generic_prefixes_still_yield_no_signature(text):
    """词典变宽最怕的是「什么都能当签名」——泛前缀必须仍然抽不出招牌。"""
    terms = brain.error_signature_terms(brain._error_voice(brain._diagnosis_span(text)))
    assert not any(w >= 3 for _, w in terms), f"泛词被当成了招牌：{terms}"


@pytest.mark.parametrize("text", [
    'Error during web fetch for "https://x/y"',
    "String to replace not found in file. 当前文件内容与 old_string 不一致",
    "File does not exist. Note: your current working directory is C:/x",
    "File has not been read yet: D:/x.py",
])
def test_harness_voice_is_live_shape(text):
    shape = brain.failure_shape(text)
    assert shape == "harness_phrase", f"宿主失败口音没被认出来：{shape}"
    assert shape in brain.FAIL_LIVE_SHAPES, "新形状必须进可上墙白名单"


def test_harness_voice_in_host_report_stderr():
    """宿主结构化报告（Bash 失败）：stdout 在前、stderr 在后，口音落在 stderr 段里。"""
    payload = ("失败调用：curl https://example.com\n"
               "Command: curl https://example.com\n"
               "Stdout: some output\n"
               "Stderr: Error during web fetch for \"https://example.com\": failed\n"
               "Exit Code: 1\n"
               "Signal: (none)")
    assert brain.failure_shape(payload) == "harness_phrase"
    assert brain._fail_harness_voice(payload)


def test_harness_voice_not_in_page_content():
    """网页正文里引用了这句口音 → 不算这次调用失败（B045/B066 的红线）。"""
    body = ("Here is the log line from the docs:\n"
            "    error during web fetch for \"https://example.com/a\"\n"
            "and then the page continues with more text about errors")
    assert not brain._fail_harness_voice(body)
    assert brain.failure_shape(body) != "harness_phrase"
    # 落在 stdout 段里也不算（那是正常输出，不是错误段）
    report = ("Command: curl x\n"
              "Stdout: error during web fetch for \"https://example.com/a\"\n"
              "Exit Code: 0\n")
    assert not brain._fail_harness_voice(report)


def test_new_phrases_are_strong_anchors():
    """新短语必须同时进锚点表——否则签名抽得出也上不了墙。"""
    for phrase in ("error during web fetch", "not a git repository",
                   "failed to receive handshake", "file does not exist"):
        assert brain._err_anchor(phrase), f"{phrase} 不是强锚点"
        assert not brain._err_weak_anchor(phrase), f"{phrase} 被当成弱锚点了"


def test_accept_gate_lets_new_signature_pass():
    """上墙判定：一条命中即可定身份（强锚点独一份）。"""
    terms = brain.error_signature_terms(
        brain._error_voice(brain._diagnosis_span(REAL_SAMPLES[0][0])))
    low = "上次踩过这个坑：error during web fetch，WebFetch 抓 GitHub 原文失败，走代理。"
    assert brain._err_accept(terms, low)
    # 反例：正文里没有这个招牌 → 不许上墙
    assert not brain._err_accept(terms, "今天天气不错，随便写点别的内容。")


def test_signature_stable_across_sessions():
    """签名键不能被会话 ID 之类漂移（会话 UUID 不是身份）。"""
    t1 = brain.error_signature_terms(brain._error_voice(
        brain._diagnosis_span('Error during web fetch for "https://a/b": failed')))
    t2 = brain.error_signature_terms(brain._error_voice(
        brain._diagnosis_span('Error during web fetch for "https://c/d": failed')))
    assert brain._error_sig_key(t1) == brain._error_sig_key(t2)


def test_session_uuid_not_used_as_identity():
    """R1019：会话 UUID 不是身份——同一个错每次撞都算新错，欠账永远收编不了。"""
    payload = ('{"session_id":"92ec29bb-b9eb-4aba-bb85-5c03af478184",'
               '"transcript_path":"C:/Users/y/.claude/x/92ec29bb-b9eb-4aba-bb85-5c03af478184.jsonl",'
               '"hook_event_name":"PostToolUseFailure","cwd":"D:/x"}')
    terms = brain.error_signature_terms(brain._error_voice(brain._diagnosis_span(payload)))
    joined = " ".join(t for t, _ in terms)
    assert "92ec29bb-b9eb-4aba-bb85-5c03af478184" not in joined
    assert "session_id" not in joined
    assert "transcript_path" not in joined
    assert "hook_event_name" not in joined


def test_uuid_in_plain_error_text_dropped():
    """正文里的 UUID 片段也不该进签名（R927 指针地址同款纪律）。"""
    terms = brain.error_signature_terms(
        'TypeError: object 92ec29bb-b9eb-4aba-bb85-5c03af478184 has no attribute foo')
    assert not any("92ec29bb" in t for t, _ in terms)
