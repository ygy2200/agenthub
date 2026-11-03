# -*- coding: utf-8 -*-
"""R852：发布脚本自己换代理——直连抛网络错时把同一个请求经候选代理再发一次。

修之前会怎样失败：本机直连 `api.github.com` 的 TLS 握手经常断（实测 curl 000，
走 `127.0.0.1:7890` 是 200），而 `release.bat` 是双击起的，不继承 shell 里设置的
`HTTPS_PROXY`。R851 发版第 5 步就是这么 ABORT 的：`publish.py` 按设计拒绝、GitHub 上
没留任何东西，但**人得手工带代理再跑一次**。这一步昨天我补了，明天就是别人踩的坑。

第一版实现是「开跑前先 GET /meta 探路」，被自己的回归闸门否掉了：`tests/unit` 里
R748/R755/R759/R761/R765 那 16 条 publish 用例按 `_call` 的**调用序列**取证，探路凭空
多发一次请求就把序列整体错位（绿的全红）。改成失败即降级后，happy path 一个额外请求
都不发——本文件第一条用例就是钉这一点的。

边界（这几条是这项改动的全部自由度，越界就成了放宽闸门）：
  · 只改「用哪条路连」，不改「连上了能不能发」——token 解析、发布闸门、预检、
    `--yes` 确认一律原样，代理通了也只是换个 opener；
  · 鉴权头必须跟着换过去的 opener 走（否则表现为「换了代理就 401」，最难查）；
  · 用户显式配了 `HTTPS_PROXY`/`ALL_PROXY` 时不再猜第二遍：真因交给预检那句话去说，
    免得脚本替用户掩盖配错的代理；
  · 一次进程里候选列表只扫一遍（`tried` 标记），否则每次网络抖动都要重跑一遍端口；
  · 只有网络层失败才降级，其它异常（代码 bug）照样冒出来。
"""
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

pub = pytest.importorskip("publish", reason="publish.py 是本地发布脚本，不入库")


@pytest.fixture(autouse=True)
def _clean_channel(monkeypatch):
    """通道记在模块级 dict 里——用例之间必须复位，否则前一条换的代理会脏了后一条。"""
    pub._reset_channel()
    monkeypatch.setattr(pub.time, "sleep", lambda _s: None)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.delenv("AGENTHUB_RELEASE_PROXIES", raising=False)
    yield
    pub._reset_channel()


def _is_proxy(opener) -> bool:
    return bool(getattr(opener, "_agenthub_proxy", ""))


def _fake(monkeypatch, outcomes):
    """按通道返回结果（元素是异常则抛出），并记录 (通道, url) 顺序。"""
    seen = []

    def fake(opener, url, method=None, data=None, timeout=120):
        channel = "proxy" if _is_proxy(opener) else "direct"
        proxy_url = getattr(opener, "_agenthub_proxy", "") or ""
        seen.append((channel, url, proxy_url))
        item = outcomes[channel] if channel in outcomes else outcomes["any"]
        if isinstance(item, Exception):
            raise item
        return item, {}, {}
    monkeypatch.setattr(pub, "_call", fake)
    return seen


def test_happy_path_sends_no_extra_request(monkeypatch):
    """直连通了就只能有一次调用：这条是 16 个 publish 用例整体变红的根因钉子。"""
    seen = _fake(monkeypatch, {"direct": 200})
    got = pub._call_retry(pub._opener("tok"), "https://api.github.com/user")
    assert got[0] == 200
    assert [s[0] for s in seen] == ["direct"], f"多发了请求：{seen}"


def test_direct_failure_reissues_same_request_via_proxy(monkeypatch, capsys):
    seen = _fake(monkeypatch, {"direct": pub.NetworkError("SSL EOF"), "proxy": 200})
    st, _b, _h = pub._call_retry(pub._opener("tok"), "https://api.github.com/user")
    assert st == 200
    assert [s[0] for s in seen] == ["direct", "proxy"], "先直连、后代理，顺序不能反"
    assert seen[0][1] == seen[1][1], "换的是同一条请求，不是另开一次探路"
    out = capsys.readouterr().out
    assert "直连 api.github.com 不通" in out and "127.0.0.1:7890" in out, out


def test_remembered_channel_is_a_real_proxy_and_keeps_token(monkeypatch):
    """换过去的 opener 既要真带 ProxyHandler，也要带鉴权头（缺后者表现为一走代理就 401）。"""
    _fake(monkeypatch, {"direct": pub.NetworkError("ssl"), "proxy": 200})
    direct = pub._opener("SECRET")
    pub._call_retry(direct, "https://api.github.com/user")
    used = pub._PROXY_CHANNEL["opener"]
    handlers = [h for h in used.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert len(handlers) == 1 and handlers[0].proxies["https"] == "http://127.0.0.1:7890"
    assert dict(used.addheaders).get("Authorization") == "Bearer SECRET"
    # 后续请求直接沿用，不再回直连试一遍
    seen = _fake(monkeypatch, {"any": 200})
    pub._call_retry(direct, "https://api.github.com/repos/x/y")
    assert [s[0] for s in seen] == ["proxy"], seen


def test_candidates_tried_only_once_per_process(monkeypatch, capsys):
    seen = _fake(monkeypatch, {"direct": pub.NetworkError("ssl"),
                               "proxy": pub.NetworkError("refused")})
    direct = pub._opener("tok")
    with pytest.raises(pub.NetworkError):
        pub._call_retry(direct, "https://api.github.com/user")
    n = len([s for s in seen if s[0] == "proxy"])
    assert n == 1, f"候选列表被扫了 {n} 遍，应只扫一遍"
    assert "都不通" in capsys.readouterr().out
    with pytest.raises(pub.NetworkError):
        pub._call_retry(direct, "https://api.github.com/user")
    assert len([s for s in seen if s[0] == "proxy"]) == n, "第二次失败不该再扫一遍候选"


def test_proxy_override_and_off_switch(monkeypatch):
    seen = _fake(monkeypatch, {"direct": pub.NetworkError("ssl"), "proxy": 200})
    monkeypatch.setenv("AGENTHUB_RELEASE_PROXIES",
                       "http://127.0.0.1:8888,http://127.0.0.1:9999")
    _st, _b, _h = pub._call_retry(pub._opener("tok"), "https://api.github.com/user")
    assert seen[1][2] == "http://127.0.0.1:8888", seen
    pub._reset_channel()
    monkeypatch.setenv("AGENTHUB_RELEASE_PROXIES", "none")
    assert pub._proxy_candidates() == [], "写 none 要能关掉这条自动降级"
    seen2 = _fake(monkeypatch, {"direct": pub.NetworkError("ssl")})
    with pytest.raises(pub.NetworkError):
        pub._call_retry(pub._opener("tok"), "https://api.github.com/user")
    assert [s[0] for s in seen2] == ["direct"] * pub.NET_RETRIES


def test_env_proxy_configured_means_no_second_guess(monkeypatch, capsys):
    """用户已经显式配了代理：脚本不猜第二条路，让预检把真因说清楚。"""
    seen = _fake(monkeypatch, {"direct": pub.NetworkError("ssl"),
                               "proxy": pub.NetworkError("不该走到")})
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1234")
    with pytest.raises(pub.NetworkError):
        pub._call_retry(pub._opener("tok"), "https://api.github.com/user")
    assert [s[0] for s in seen] == ["direct"] * pub.NET_RETRIES, "配了代理就不该去试候选端口"
    assert "不猜第二条路" in capsys.readouterr().out


def test_foreign_opener_is_not_rerouted(monkeypatch):
    """降级只认本模块 `_opener()` 造的连接。

    R765 那批用例把 `_opener` 换成假对象、R718 的预检直接传 None：它们测的是重试
    次数与文案，不是代理。要是这里照样去扫候选，装置就会拿 `_opener(token, proxy=…)`
    撞真网络（并把按序列取证的用例整体错位——R852 第一版就是这么红了 16 条的）。
    """
    class Foreign:
        """没有 _agenthub_proxy / _agenthub_token 字段的连接：不属于本模块。"""

    seen = _fake(monkeypatch, {"direct": pub.NetworkError("ssl"),
                               "proxy": pub.NetworkError("不该走到")})
    with pytest.raises(pub.NetworkError):
        pub._call_retry(Foreign(), "https://api.github.com/user")
    assert [s[0] for s in seen] == ["direct"] * pub.NET_RETRIES, seen


def test_non_network_error_is_not_swallowed(monkeypatch):
    """只有网络层失败才降级：代码 bug（其它异常）必须照样冒出来，不能被「换代理」掩盖。"""
    def boom(opener, url, method=None, data=None, timeout=120):
        raise ValueError("写错了")
    monkeypatch.setattr(pub, "_call", boom)
    with pytest.raises(ValueError):
        pub._call_retry(pub._opener("tok"), "https://api.github.com/user")


def test_preflight_still_gates_token_after_fallback(monkeypatch):
    """降级只解决「连不上」：连上了而 token 不行，预检依旧不通过（闸门没被顺手放宽）。"""
    def fake(opener, url, method=None, data=None, timeout=120):
        if not _is_proxy(opener):
            raise pub.NetworkError("直连不通")
        if url.endswith("/user"):
            return 401, {"message": "Bad credentials"}, {}
        return 404, {}, {}
    monkeypatch.setattr(pub, "_call", fake)
    pf = pub.preflight(pub._opener("tok"), "ygy2200/agenthub")
    assert pf["ok"] is False, pf
    assert any("401" in p for p in pf["problems"]), pf["problems"]
    assert pub._PROXY_CHANNEL["name"].startswith("代理")


def test_channel_label_reflects_what_actually_happened(monkeypatch):
    direct = pub._opener("tok")
    assert pub._channel_label(direct) == "直连"
    _fake(monkeypatch, {"direct": pub.NetworkError("ssl"), "proxy": 200})
    pub._call_retry(direct, "https://api.github.com/user")
    assert pub._channel_label(direct) == "代理 http://127.0.0.1:7890"
