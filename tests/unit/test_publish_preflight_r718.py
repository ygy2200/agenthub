# -*- coding: utf-8 -*-
"""R718：发布前预检 —— 坏 token / 断网 / 无权限要在建 Release 之前就说清楚。

修之前会怎样失败：`publish.py --yes` 先建 Release 再上传 exe。token 过期时 GitHub 在
`POST /releases` 就回 401，报错信息是裸的 `HTTP Error 401`；更糟的是闸门全过、token
半有效（能读不能写）时，会留下一个**没有 exe 的 Release**，只能手工去网页删。
预检把「我是谁」「这个仓库推得动吗」「网络通不通」三类失败分开报，且一次写请求都不发。

注：`publish.py` 是本地发布脚本（.gitignore），CI 上不存在 → 本文件自动跳过。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

pub = pytest.importorskip("publish", reason="publish.py 是本地发布脚本，不入库")


def _seq(monkeypatch, results):
    """按调用顺序返回预设的 (status, body, headers)；元素是异常则抛出。"""
    calls = list(results)

    def fake(opener, url, method=None, data=None, timeout=120):
        item = calls.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(pub, "_call", fake)


def test_happy_path_reports_login_and_scope(monkeypatch):
    _seq(monkeypatch, [(200, {"login": "ygy2200"}, {"X-OAuth-Scopes": "repo"}),
                       (200, {"full_name": "ygy2200/agenthub",
                              "permissions": {"push": True}}, {})])
    pf = pub.preflight(opener=None, repo="ygy2200/agenthub")
    assert pf["ok"] and pf["login"] == "ygy2200" and pf["scopes"] == "repo"


def test_expired_token_says_expired_not_http_401(monkeypatch):
    _seq(monkeypatch, [(401, {"message": "Bad credentials"}, {}),
                       (404, {}, {})])
    pf = pub.preflight(None, "ygy2200/agenthub")
    assert not pf["ok"]
    joined = " ".join(pf["problems"])
    assert "401" in joined and "过期" in joined
    assert "credentials.json" in joined  # 必须给出去哪儿换 token


def test_token_without_write_permission(monkeypatch):
    _seq(monkeypatch, [(200, {"login": "x"}, {}),
                       (200, {"permissions": {"push": False}}, {})])
    pf = pub.preflight(None, "ygy2200/agenthub")
    assert not pf["ok"]
    assert any("写权限" in p for p in pf["problems"])


def test_missing_repo_is_reported_as_scope_or_name(monkeypatch):
    _seq(monkeypatch, [(200, {"login": "x"}, {}), (404, {}, {})])
    pf = pub.preflight(None, "ygy2200/nope")
    assert not pf["ok"]
    assert any("404" in p and "权限" in p for p in pf["problems"])


def test_network_failure_hints_proxy(monkeypatch):
    """直连 api.github.com 在本机会 SSL 断——提示必须给出实测可用的代理写法。"""
    _seq(monkeypatch, [pub.NetworkError("SSL: UNEXPECTED_EOF_WHILE_READING")])
    pf = pub.preflight(None, "ygy2200/agenthub")
    assert not pf["ok"]
    assert any("7890" in p for p in pf["problems"])


def test_preflight_does_not_write(monkeypatch):
    """预检只允许 GET：任何 POST/PUT/DELETE 都说明它已经开始动手了。"""
    seen = []

    def fake(opener, url, method=None, data=None, timeout=120):
        seen.append((method, url))
        if url.endswith("/user"):
            return 200, {"login": "x"}, {}
        return 200, {"permissions": {"push": True}}, {}

    monkeypatch.setattr(pub, "_call", fake)
    pub.preflight(None, "ygy2200/agenthub")
    assert all(m is None for m, _ in seen), seen
