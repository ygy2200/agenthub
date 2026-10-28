# -*- coding: utf-8 -*-
"""明文凭据预筛与巡检批内审计链只验一次（R247）。

真实库实测 security_health 217.8ms = 审计链校验 69.7 + 明文扫描 136.0；扫描里
正则占 118.6ms（2027 条 / 118 万字）。两条都不改判定：

  ① 先过「任何模式命中都必需的字面量」再跑正则——真实库过筛 13%，正则 118.6→26.1ms；
  ② 整批巡检是一个刷新域：`audit_chain` 与 `security_health` 共用同一次链校验。

不变量：
  · 预筛只能跳过**不可能命中**的文本，任何一条的判定都不许变（含占位符/环境变量/
    Unicode 边界）；
  · 批内只验一次，但链被改坏时两个检查都必须照样报红（省的是重复，不是检测）。
"""
import random

import pytest

import brain
import ops

CLEAN = [
    "普通中文记录，没有密钥",
    "the task description: nothing",
    "это секрет и пароль",
    "パスワード: 12345678",
    "他是秘密守护者",
    "",
]


def _legacy_label(text):
    """R246 及以前的实现（无预筛），用于对拍「预筛没有改变任何判定」。"""
    for label, pat in ops._SECRET_PATTERNS:
        m = pat.search(text or "")
        if not m:
            continue
        tok = m.group(1) if label == "明文口令赋值" else m.group(0)
        if ops._looks_placeholder(tok):
            continue
        if label == "明文口令赋值":
            v = (m.group(1) or "").strip()
            low = v.lower()
            if (low.startswith(("os.environ", "getenv"))
                    or v[0] in "$%{<" or set(v) <= set("*x.-_")):
                continue
        return label
    return ""


def test_clean_texts_are_screened_out():
    for s in CLEAN:
        assert ops._may_contain_secret(s) is False, s
        assert ops._secret_label(s) == ""


def test_screen_is_necessary_condition_for_every_label():
    """预筛的必要性：所有能命中的文本都必过筛；跳过一律等于「干净」。"""
    samples = [
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
        "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWX",
        "AKIAIOSFODNN7EXAMPLX",
        "-----BEGIN RSA PRIVATE KEY-----",
        "PASSWORD: hunter2hunter2",
        "passwd:correct-horse-battery",
        "secret=hunter2hunter2",
        "api_key: 1234567890abcdef",
        "APIKEY:1234567890abcdef",
        "access_token: ya29abcdefghij",
        "ACCESSTOKEN=ya29abcdefghij",
    ] + CLEAN
    for s in samples:
        label = ops._secret_label(s)
        assert label == _legacy_label(s), s
        if label:
            assert ops._may_contain_secret(s) is True, s


def test_screen_matches_legacy_on_fuzz():
    """随机拼词对拍：预筛后的判定必须与无预筛实现逐条相同。"""
    pieces = ["password", "PASSWORD", "Password", "passwd", "passphrase", "secret",
              "api_key", "api-key", "APIKEY", "access_token", "access-token",
              "ACCESSTOKEN", "sk-", "sk-ant-", "ghp_", "AKIA", "-----BEGIN ",
              "PRIVATE KEY-----", "=", ":", " ", "'", '"', "hunter2hunter2",
              "12345678", "1234567", "os.environ", "$PW", "%TOK%", "xxxxxxxx",
              "your", "example", "here", "中文", "àé", "-", "_", "\n", "/", "@"]
    rng = random.Random(20261028)
    for _ in range(400):
        s = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 12)))
        assert ops._secret_label(s) == _legacy_label(s), repr(s)


def _spy_verify(monkeypatch):
    calls = []
    real = brain.journal_verify

    def spy(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(brain, "journal_verify", spy)
    return calls


def test_patrol_verifies_chain_once(hub_root, monkeypatch):
    brain.init_db(hub_root)
    calls = _spy_verify(monkeypatch)
    rep = ops.run_patrol(hub_root, checks=("audit_chain", "security_health"))
    assert len(calls) == 1
    assert rep["checked"] == 2
    assert rep["issues"] == []


def test_single_checks_still_verify_each_time(hub_root, monkeypatch):
    brain.init_db(hub_root)
    calls = _spy_verify(monkeypatch)
    ops.run_check("audit_chain", "", hub_root)
    ops.run_check("security_health", "", hub_root)
    assert len(calls) == 2


def test_patrol_scope_does_not_leak(hub_root):
    brain.init_db(hub_root)
    ops.run_patrol(hub_root, checks=("audit_chain",))
    assert ops._REFRESH.get() is None


def test_patrol_still_flags_broken_chain_on_both_checks(hub_root):
    """批内复用同一次校验，但链坏了两个检查都得报红——省的是重复不是检测。"""
    brain.init_db(hub_root)
    brain.journal_add(hub_root, "ops", "检查 x", note="[ok] y")
    with brain.db_conn(hub_root) as conn:
        jid = conn.execute("SELECT id FROM journal WHERE hash!='' "
                           "ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.execute("UPDATE journal SET note='被改' WHERE id=?", (jid,))
    rep = ops.run_patrol(hub_root, checks=("audit_chain", "security_health"))
    names = {i["name"] for i in rep["issues"]}
    assert {"audit_chain", "security_health"} <= names


@pytest.mark.parametrize("where", ["records", "memories"])
def test_scan_still_finds_planted_secret(hub_root, where):
    """端到端：预筛不许让真密钥漏过 _scan_plaintext_secrets。"""
    brain.init_db(hub_root)
    if where == "records":
        brain.add_record(hub_root, "proj", "qoder", "2026-10-28", "标题",
                         "口令 password: hunter2hunter2")
    else:
        brain.add_memory(hub_root, "口令 password: hunter2hunter2", "fact")
    hits, scanned = ops._scan_plaintext_secrets(hub_root, brain)
    assert scanned > 0
    assert [h["kind"] for h in hits] == [("记录" if where == "records" else "记忆")]
    assert "hunter2" not in str(hits)
