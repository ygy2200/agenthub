# -*- coding: utf-8 -*-
"""刷新域去重与明文扫描缓存（R242）。

治理页 security 段一次可见刷新 325ms，量出来两块白付：
  ① 同一条审计链校验跑两遍（`_SecuritySub._fill` 一次、`_security_health` 里再一次，68ms）；
  ② 明文凭据扫描 132ms，逐行扫 5500 条，而多数刷新之间被扫的表根本没变。

守一条不变量：**缓存只许省重复劳动，不许让页面看到旧数**。
  · 刷新域内同一份库只验一次；域外（CLI/MCP）每次真验，行为不许变；
  · 被扫表新增/改长度 → 签名变 → 必须重扫；
  · 本页自己写 journal 流水**不该**让扫描缓存失效——那正是第一版用库文件 mtime
    做键时热态仍有 254ms 的根因（每刷一次都写 4 条落痕）。
"""
import pytest

import brain
import ops


@pytest.fixture(autouse=True)
def _clear_scan_cache():
    ops._SECRET_SCAN_CACHE.clear()
    yield
    ops._SECRET_SCAN_CACHE.clear()


def _spy_verify(monkeypatch):
    calls = []
    real = brain.journal_verify

    def spy(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(brain, "journal_verify", spy)
    return calls


def test_refresh_scope_dedupes_verify(hub_root, monkeypatch):
    brain.init_db(hub_root)
    calls = _spy_verify(monkeypatch)

    with ops.refresh_scope():
        a = ops.verify_cached(hub_root)
        b = ops.verify_cached(hub_root)

    assert len(calls) == 1, "一次刷新里审计链只该校验一次"
    assert a == b


def test_outside_scope_every_call_verifies(hub_root, monkeypatch):
    brain.init_db(hub_root)
    calls = _spy_verify(monkeypatch)

    ops.verify_cached(hub_root)
    ops.verify_cached(hub_root)

    assert len(calls) == 2, "CLI/MCP 没有刷新域，不许被顺手改成缓存"


def test_nested_scopes_share_one_domain(hub_root, monkeypatch):
    brain.init_db(hub_root)
    calls = _spy_verify(monkeypatch)

    with ops.refresh_scope():
        ops.verify_cached(hub_root)
        with ops.refresh_scope():
            ops.verify_cached(hub_root)

    assert len(calls) == 1, "页级刷新套段级刷新要共用一个域"


def test_security_health_reuses_scope_verify(hub_root, monkeypatch):
    brain.init_db(hub_root)
    calls = _spy_verify(monkeypatch)

    with ops.refresh_scope():
        ops.verify_cached(hub_root, brain)
        res = ops._security_health(hub_root)

    assert res["ok"] is True, res
    assert len(calls) == 1, "段首行与体检内部共付的那次校验只该算一次"


def test_security_health_alone_still_verifies(hub_root, monkeypatch):
    brain.init_db(hub_root)
    calls = _spy_verify(monkeypatch)

    ops._security_health(hub_root)

    assert len(calls) == 1, "域外调用必须照旧真验（巡检/CLI 的口径不变）"


def test_journal_write_keeps_scan_signature(hub_root):
    brain.init_db(hub_root)
    before = ops._scan_signature(hub_root, brain)

    brain.journal_add(hub_root, "qoder", "r242-signature-probe", "signature")

    after = ops._scan_signature(hub_root, brain)
    assert after == before, "本页写流水不该让明文扫描缓存失效"


def test_note_write_changes_scan_signature(hub_root):
    brain.init_db(hub_root)
    before = ops._scan_signature(hub_root, brain)

    brain.add_note(hub_root, "一条普通随记")

    assert ops._scan_signature(hub_root, brain) != before


def test_scan_cache_hits_then_invalidates(hub_root, monkeypatch):
    brain.init_db(hub_root)
    calls = []
    real = ops._scan_plaintext_secrets

    def spy(root, b):
        calls.append(root)
        return real(root, b)

    monkeypatch.setattr(ops, "_scan_plaintext_secrets", spy)

    ops._scan_plaintext_secrets_cached(hub_root, brain)
    ops._scan_plaintext_secrets_cached(hub_root, brain)
    assert len(calls) == 1, "库没变时不该重扫"

    brain.add_note(hub_root, "又一条随记")
    ops._scan_plaintext_secrets_cached(hub_root, brain)
    assert len(calls) == 2, "库变了必须重扫"


def test_cached_scan_sees_newly_added_secret(hub_root):
    brain.init_db(hub_root)
    ops._scan_plaintext_secrets_cached(hub_root, brain)

    brain.add_note(hub_root, "临时口令 password = Zx9Kq2Lm8Pw4Rt6Y")
    hits, _scanned = ops._scan_plaintext_secrets_cached(hub_root, brain)

    assert [h for h in hits if h["kind"] == "随记"], "新写入的明文口令必须被下一次扫描看到"


def test_scan_signature_survives_many_journal_writes(hub_root):
    """连续刷新会不断写流水；签名必须稳定，否则切片等于没做（R242 实测热态 254ms）。"""
    brain.init_db(hub_root)
    base = ops._scan_signature(hub_root, brain)
    for i in range(4):
        brain.journal_add(hub_root, "qoder", f"refresh-{i}", "signature")
    assert ops._scan_signature(hub_root, brain) == base
