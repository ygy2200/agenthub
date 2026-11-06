# -*- coding: utf-8 -*-
"""R921：附件上传也必须走「已换好的代理通道」——发布链的第 5 步不能再靠人工补。

实锤现场（2026-11-06，v2.41.244）：`release.bat` 第 5 步日志先打印
「网络：直连 api.github.com 不通，这个请求改走本机代理 127.0.0.1:7890（后续请求沿用）」，
紧接着 `exe 上传失败：[WinError 10060]`——通道明明已经换好并宣布「后续请求沿用」，
上传却仍是直连。原因很直白：R852 的降级挂在 `_call_retry`/`_call` 那条路上，
而 `_upload_asset` 用的是外面传进来的原始 `opener` 直接 POST `uploads.github.com`。
后果是 Release 停在草稿（公开页看不到），`release.bat` ABORT，必须人再跑一遍
`--upload-only`——正是 R852 立誓要消灭的那种「这一步昨天我手工补了」的坑。

口径（改动只有连接层，闸门一条不放宽）：
  ① 上传走 `_route(opener)`：已经换过通道就一直用那条，与 API 请求同一事实源；
  ② 上传自己失败时也允许降级（api 通而 uploads 不通是真实形态）：扫候选代理，
     通了就记住通道、把这一份附件传完，**不额外消耗尝试次数**；
  ③ 准入与 `_via_proxy` 完全一致：只认本模块 `_opener()` 造的连接、用户显式配了
     环境变量代理时不猜第二条路、候选列表一个进程只扫一遍；
  ④ 换路后仍失败（对方给 4xx/5xx）就照实报失败——换路只解决「连不上」。
"""
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

pub = pytest.importorskip("publish", reason="publish.py 是本地发布脚本，不入库")

UPLOAD_URL = "https://uploads.github.com/repos/o/r/releases/42/assets{?name,label}"
OK_BODY = {"state": "uploaded", "browser_download_url": "https://dl/a.exe"}


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """plan 里每个元素是一次 POST 的结局：("ok") / ("net", 原因) / ("http", 码)。"""

    def __init__(self, plan=(), proxy="", token="tok"):
        self.plan = list(plan)
        self.posts = []
        self._agenthub_proxy = proxy
        self._agenthub_token = token

    def open(self, req, timeout=None):
        self.posts.append((req.get_method().upper(), req.full_url))
        item = self.plan.pop(0) if self.plan else ("ok", None)
        kind = item[0]
        if kind == "net":
            raise urllib.error.URLError(item[1])
        if kind == "http":
            raise urllib.error.HTTPError(req.full_url, item[1], "err", {},
                                         io.BytesIO(b"{}"))
        return _Resp(201, dict(OK_BODY))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pub._reset_channel()
    monkeypatch.setattr(pub.time, "sleep", lambda _s: None)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.delenv("AGENTHUB_RELEASE_PROXIES", raising=False)
    monkeypatch.delenv("AGENTHUB_UPLOAD_RETRIES", raising=False)
    # 附件表对账是 R765 那条线的事，本文件只盯连接层；置空 = 远端没有同名附件
    monkeypatch.setattr(pub, "_find_asset", lambda *a, **k: None)
    yield
    pub._reset_channel()


@pytest.fixture
def exe(tmp_path):
    f = tmp_path / "AgentHub.exe"
    f.write_bytes(b"x" * 4096)
    return f


def _run(direct, **kw):
    return pub._upload_asset(direct, UPLOAD_URL, "https://api.github.com/repos/o/r",
                             42, "AgentHub.exe", exe=kw["exe"], timeout=30)[0]


def test_upload_reuses_channel_already_switched_by_api(exe, monkeypatch):
    """API 请求已经换到代理，上传就不许再拿直连 opener 撞 uploads.github.com。

    修前必红：`_upload_asset` 用原始 opener POST，直连那台被记了一次 10060 式失败。
    """
    direct = FakeOpener([("net", "WinError 10060")])
    proxy = FakeOpener([("ok", None)], proxy="http://127.0.0.1:7890")
    pub._PROXY_CHANNEL.update({"opener": proxy, "name": "代理 http://127.0.0.1:7890",
                               "tried": True})
    monkeypatch.setattr(pub, "_opener", lambda token, proxy="": FakeOpener(
        [("ok", None)], proxy=proxy))
    asset = _run(direct, exe=exe)
    assert asset.get("browser_download_url") == OK_BODY["browser_download_url"], asset
    assert direct.posts == [], f"上传不该再碰直连：{direct.posts}"
    assert [p[1] for p in proxy.posts] == [UPLOAD_URL], proxy.posts


def test_upload_direct_failure_reroutes_and_finishes_in_one_attempt(exe, monkeypatch,
                                                                    capsys):
    """api 通、uploads 不通的真实形态：上传自己降级，且不白烧一次尝试数。"""
    direct = FakeOpener([("net", "connection reset")])
    built = []

    def factory(token, proxy=""):
        op = FakeOpener([("ok", None)], proxy=proxy, token=token)
        built.append(op)
        return op

    monkeypatch.setattr(pub, "_opener", factory)
    asset = _run(direct, exe=exe)
    assert asset.get("browser_download_url"), asset
    assert len(built) == 1, "候选代理只该扫一遍"
    assert built[0]._agenthub_proxy == "http://127.0.0.1:7890"
    assert built[0]._agenthub_token == "tok", "换路必须带着鉴权头，否则表现为 401"
    assert len(direct.posts) == 1, f"降级后不该回直连重试：{direct.posts}"
    assert pub._channel_label(direct).startswith("代理"), pub._channel_label(direct)
    out = capsys.readouterr().out
    assert "改走本机代理" in out and "127.0.0.1:7890" in out, out


def test_switched_channel_is_used_by_later_attempts(exe, monkeypatch):
    """换路成功后，后续尝试沿用同一条通道——每次都回直连等于把失败重试两遍。"""
    direct = FakeOpener([("net", "reset"), ("net", "reset")])
    made = []

    def factory(token, proxy=""):
        op = FakeOpener([("http", 503), ("ok", None)], proxy=proxy)
        made.append(op)
        return op

    monkeypatch.setattr(pub, "_opener", factory)
    asset = _run(direct, exe=exe)
    assert asset.get("browser_download_url"), asset
    assert len(direct.posts) == 1, f"换过通道后第 2 次仍走直连：{direct.posts}"
    assert len(made[0].posts) == 2


def test_env_proxy_configured_means_no_second_guess(exe, monkeypatch, capsys):
    """用户显式配了代理：不猜第二遍端口，失败照实上报（与 _via_proxy 同一条纪律）。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1234")
    direct = FakeOpener([("net", "reset"), ("net", "reset")])
    built = []
    monkeypatch.setattr(pub, "_opener", lambda token, proxy="": built.append(
        proxy) or FakeOpener([("ok", None)], proxy=proxy))
    asset = _run(direct, exe=exe)
    assert asset.get("error"), asset
    assert built == [], "配了代理就不该去试候选端口"
    assert len(direct.posts) == 2, "按原路重试，尝试数照旧"


def test_foreign_opener_is_not_rerouted(exe, monkeypatch):
    """降级只认本模块造的连接：装置传的假 opener 没有 token，替它换路会撞真网络。"""

    class Foreign:
        def __init__(self):
            self.posts = []

        def open(self, req, timeout=None):
            self.posts.append(req.full_url)
            raise urllib.error.URLError("reset")

    foreign = Foreign()
    built = []
    monkeypatch.setattr(pub, "_opener", lambda token, proxy="": built.append(
        proxy) or FakeOpener([("ok", None)], proxy=proxy))
    asset = _run(foreign, exe=exe)
    assert asset.get("error")
    assert built == [], "外来的连接不该被脚本换成代理"


def test_candidates_exhausted_reports_original_failure(exe, monkeypatch, capsys):
    """候选全不通：照实失败，别把「换了路也没通」说成成功。"""
    direct = FakeOpener([("net", "直连不通"), ("net", "直连不通")])
    monkeypatch.setattr(pub, "_opener", lambda token, proxy="": FakeOpener(
        [("net", "代理也不通")], proxy=proxy))
    asset = _run(direct, exe=exe)
    assert asset.get("error") == "直连不通", asset
    assert "都不通" in capsys.readouterr().out


def test_reroute_still_reports_http_error_from_github(exe, monkeypatch):
    """换路只解决「连不上」：代理通了而 GitHub 持续给 5xx，仍然是失败，且通道保留。"""
    direct = FakeOpener([("net", "reset"), ("net", "reset")])
    monkeypatch.setattr(pub, "_opener", lambda token, proxy="": FakeOpener(
        [("http", 503), ("http", 503)], proxy=proxy))
    asset = _run(direct, exe=exe)
    assert asset.get("error", "").startswith("HTTP 503"), asset
    assert pub._PROXY_CHANNEL["opener"] is not None, "通道是通的，下一轮该沿用"
    assert len(direct.posts) == 1, f"第 2 次不该回直连：{direct.posts}"
