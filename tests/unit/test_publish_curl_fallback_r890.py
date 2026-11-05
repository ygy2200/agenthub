# -*- coding: utf-8 -*-
"""R890 / B076：环境变量代理下 urllib 传输栈失败时，用 curl 走同一条代理兜底。

R885 实测把定性纠正过：`publish.py --check` 首次报 SSL EOF 是**偶发抖动**
（urllib 同代理 3/3 200、curl 200、git ls-remote 通、二次 --check 通过），
不是代理故障也不是稳定缺陷。真缺口只是**韧性**：R852 的 `_via_proxy` 在
`_env_proxy_set()` 为真时直接短路「不猜第二条路」，抖动耗尽 NET_RETRIES 后
没有任何别的传输栈兜底，只能靠人重跑。

边界（越界就成了放宽闸门）：
  · 只换**传输栈**，不换代理、不改 token/闸门/预检/--yes；
  · happy path 一个额外请求都不发（R852 的 16 条按调用序列取证的用例全盯着这点）；
  · 只有网络层失败才兜底；HTTP 4xx/5xx 如实返回，不许伪装成网络抖动继续重试；
  · curl 也失败就如实报错，不静默。

本轮只覆盖 GitHub API 调用；49MB 附件上传路径仍走既有
`tools/upload_via_curl_r861.py` 人工补传，标**待确认**（不假装做完）。
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
pub = pytest.importorskip("publish", reason="publish.py 是本地发布脚本，不入库")

CURL_OK = b'{"login":"ygy2200"}\n__AGENTHUB_HTTP_STATUS__200'
PROXY = "http://127.0.0.1:7890"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pub._reset_channel()
    monkeypatch.setattr(pub.time, "sleep", lambda _s: None)
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    for k in ("https_proxy", "ALL_PROXY", "all_proxy", "AGENTHUB_RELEASE_PROXIES"):
        monkeypatch.delenv(k, raising=False)
    # 本文件模拟的正是「用户已显式配了代理」这一支（R852 在此短路）。
    # 直接钉住判据，免得宿主/conftest 的环境变量差异让用例前提悄悄失效。
    monkeypatch.setattr(pub, "_env_proxy_set", lambda: True)
    yield
    pub._reset_channel()


def _fake_call(monkeypatch, outcome):
    """替换 _call；outcome 是异常就抛，否则当 (status, body, headers) 的 status。"""
    seen = []

    def fake(opener, url, method=None, data=None, timeout=120):
        seen.append((url, method))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, {}, {}
    monkeypatch.setattr(pub, "_call", fake)
    return seen


def _curl_result(cmd, status=200, payload=b'{"login":"ygy2200"}', rc=0, err=b""):
    return subprocess.CompletedProcess(
        cmd, rc, stdout=payload + b"\n__AGENTHUB_HTTP_STATUS__" + str(status).encode(),
        stderr=err)


def test_network_error_falls_back_to_curl_same_proxy(monkeypatch, capsys):
    """urllib 抛 NetworkError → 同一个请求用 curl 同代理发出去，鉴权头要跟着走。"""
    seen = _fake_call(monkeypatch, pub.NetworkError("SSL EOF"))
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _curl_result(cmd)

    monkeypatch.setattr(pub.subprocess, "run", fake_run)
    st, body, _ = pub._call_retry(pub._opener("SECRET"), "https://api.github.com/user")
    assert st == 200 and body["login"] == "ygy2200"
    assert len(seen) == 1, "同一个请求只发一次，不预先探路"
    assert len(calls) == 1
    assert any("Bearer SECRET" in str(c) for c in calls[0]), "换栈不能丢鉴权头"
    assert "--proxy" in calls[0] and PROXY in calls[0], "必须走同一条代理，不是另猜一条"
    assert "curl" in capsys.readouterr().out


def test_happy_path_never_invokes_curl(monkeypatch):
    """直连/urllib 通了就一个额外请求都不许发（R852 16 条序列用例的钉子）。"""
    _fake_call(monkeypatch, 200)

    def boom(*a, **k):
        raise AssertionError("happy path 不该调 curl")

    monkeypatch.setattr(pub.subprocess, "run", boom)
    assert pub._call_retry(pub._opener("tok"), "https://api.github.com/user")[0] == 200


def test_http_error_is_not_downgraded(monkeypatch):
    """HTTP 404 是「连上了」，不许当网络抖动去换栈重试。"""
    seen = _fake_call(monkeypatch, 404)

    def boom(*a, **k):
        raise AssertionError("HTTP 错误不该触发 curl")

    monkeypatch.setattr(pub.subprocess, "run", boom)
    assert pub._call_retry(pub._opener("tok"), "https://api.github.com/user")[0] == 404
    assert len(seen) == 1


def test_curl_http_status_is_returned_not_retried(monkeypatch):
    """curl 拿到 401 要如实交给预检报 token 问题，不能伪装成网络错继续重试。"""
    seen = _fake_call(monkeypatch, pub.NetworkError("SSL EOF"))
    monkeypatch.setattr(pub.subprocess, "run",
                        lambda cmd, **kw: _curl_result(cmd, status=401,
                                                       payload=b'{"message":"Bad credentials"}'))
    st, body, _ = pub._call_retry(pub._opener("BAD"), "https://api.github.com/user")
    assert st == 401 and "Bad credentials" in str(body)
    assert len(seen) == 1, "401 不该触发第二轮重试"


def test_curl_failure_raises_and_is_visible(monkeypatch, capsys):
    """curl 也失败：如实抛 NetworkError，输出点名，不静默吞掉。"""
    _fake_call(monkeypatch, pub.NetworkError("SSL EOF"))
    monkeypatch.setattr(pub.subprocess, "run",
                        lambda cmd, **kw: _curl_result(cmd, rc=7, payload=b"", err=b"connect refused"))
    with pytest.raises(pub.NetworkError):
        pub._call_retry(pub._opener("tok"), "https://api.github.com/user")
    assert "curl" in capsys.readouterr().out


def test_method_and_body_are_forwarded(monkeypatch):
    """POST + JSON body 要原样透传，不能把「同一个请求」换成 GET。"""
    _fake_call(monkeypatch, pub.NetworkError("SSL EOF"))
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["input"] = kw.get("input")
        return _curl_result(cmd, status=201, payload=b'{"id":1}')

    monkeypatch.setattr(pub.subprocess, "run", fake_run)
    st, _, _ = pub._call_retry(pub._opener("tok"), "https://api.github.com/repos/x/y/releases",
                               method="POST", data={"tag_name": "v1"})
    assert st == 201
    assert "-X" in captured["cmd"] and "POST" in captured["cmd"]
    assert b"tag_name" in (captured["input"] or b""), "body 没带上"
