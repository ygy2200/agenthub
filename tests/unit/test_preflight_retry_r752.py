# -*- coding: utf-8 -*-
"""R752：一次网络抖动被读成「预检未通过」。

实测（2026-10-30 本机 `publish.py --check`）：
    预检：身份 ygy2200，scope …
      - 连不上 api.github.com（[SSL: UNEXPECTED_EOF_WHILE_READING] …）
    预检  未通过
    Release 体检  空附件版本：0 个
同一次运行里，带重试的 Release 体检成功了——说明网络是好的，只是那一次握手断了。
`preflight` 没有重试，于是「偶发」被报成「必然」，用户会去换 token/代理白折腾。
口径：网络层失败重试（有上限），HTTP 层失败不重试（401/404 重试一万次也一样）。
"""
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import publish  # noqa: E402


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._raw = publish.json.dumps(payload).encode()
        self.headers = {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    """前 fail_first 次 open 抛 URLError，之后按 URL 正常回放。"""

    def __init__(self, fail_first=0, status=200):
        self.fail_first = fail_first
        self.status = status
        self.n = 0

    def open(self, req, timeout=None):
        self.n += 1
        if self.n <= self.fail_first:
            raise urllib.error.URLError("SSL: UNEXPECTED_EOF_WHILE_READING")
        if req.full_url.endswith("/user"):
            return _Resp(self.status, {"login": "me"})
        return _Resp(self.status, {"permissions": {"push": True}})


def test_one_blip_does_not_fail_preflight(monkeypatch):
    monkeypatch.setattr(publish, "NET_BACKOFF", 0)
    op = Opener(fail_first=1)
    pf = publish.preflight(op, "o/r")
    assert pf["ok"] and pf["login"] == "me"
    assert op.n == 3, "第一次失败后应重试，而不是直接判死"


def test_permanent_outage_still_reports_network(monkeypatch):
    monkeypatch.setattr(publish, "NET_BACKOFF", 0)
    op = Opener(fail_first=99)
    pf = publish.preflight(op, "o/r")
    assert not pf["ok"] and any("7890" in p for p in pf["problems"])
    assert op.n == publish.NET_RETRIES, "重试要有上限，不能无限重连"


def test_http_401_is_not_retried(monkeypatch):
    monkeypatch.setattr(publish, "NET_BACKOFF", 0)
    op = Opener(fail_first=0, status=401)
    pf = publish.preflight(op, "o/r")
    assert not pf["ok"]
    assert op.n == 2, "401 不是网络故障：/user 与 /repos 各问一次就够"
