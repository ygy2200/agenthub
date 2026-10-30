# -*- coding: utf-8 -*-
r"""R765：96MB 上传中断后只能人工救——补齐有限重试与「响应丢了」对账。

现状（R742/R748 之后）：上传失败会打印进度与 `--upload-only` 恢复命令，但**不会自己再试**。
慢代理下一次抖动就要人或等下一轮发版；更隐蔽的是「POST 已经落库、响应在回程丢了」——
这时重传撞 422 already_exists，或上一轮「先删同名附件再传」把刚传好的文件删掉重来。

口径：
  ① 网络层/5xx 中断 → 最多 `AGENTHUB_UPLOAD_RETRIES`（默认 2）次，每次从 0 重传；
  ② 每次失败后先查远端附件表：同名且 size 一致 = 其实传好了，按成功处理，不再重传；
  ③ 同名但 size 不符 = 旧的/半截的，删掉再传；
  ④ 全部尝试失败 → 退出码 1、Release 留在草稿、给 `--upload-only` 恢复命令。
"""
import io
import json
import re
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import publish  # noqa: E402

REL = "https://api.github.com/repos/o/r/releases"
TAG = "v9.9.9"
ASSET = f"AgentHub-{TAG}.exe"


class _Resp:
    def __init__(self, status, payload, headers=None):
        self.status = status
        self._raw = json.dumps(payload).encode()
        self.headers = headers or {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


NET = ("net", "timed out")
HTTP503 = ("http", 503)

_asset = {"id": 5, "name": ASSET, "size": 4096, "state": "uploaded",
          "browser_download_url": "https://dl/a.exe"}


class Opener:
    """upload_plan 消费上传尝试；assets_plan 消费每次失败后的附件表查询。"""

    def __init__(self, upload_plan, assets_plan=()):
        self.upload_plan = list(upload_plan)
        self.assets_plan = list(assets_plan)
        self.calls = []

    def count(self, method, prefix=""):
        return sum(1 for m, u in self.calls if m == method and u.startswith(prefix))

    def open(self, req, timeout=None):
        url = req.full_url
        method = req.get_method().upper()
        self.calls.append((method, url))
        if url.endswith("/user"):
            return _Resp(200, {"login": "me"}, {"X-OAuth-Scopes": "repo"})
        if re.search(r"/repos/o/r$", url):
            return _Resp(200, {"permissions": {"push": True}})
        if "/releases/tags/" in url:
            return _Resp(404, {"message": "Not Found"})
        if method == "POST" and url.endswith("/releases"):
            return _Resp(201, {"id": 42, "draft": True,
                               "upload_url": "https://uploads.github.com/u{?name,label}"})
        if re.search(r"/releases/\d+/assets$", url):
            return _Resp(200, self.assets_plan.pop(0) if self.assets_plan else [])
        if method == "DELETE" and "/releases/assets/" in url:
            return _Resp(204, {})
        if url.startswith("https://uploads.github.com"):
            step = self.upload_plan.pop(0) if self.upload_plan else ("ok", None)
            if step[0] == "ok":
                return _Resp(201, {"state": "uploaded",
                                   "browser_download_url": "https://dl/a.exe"})
            if step[0] == "http":
                raise urllib.error.HTTPError(url, step[1], "err", {},
                                             io.BytesIO(b"{}"))
            raise urllib.error.URLError(step[1])
        if method == "PATCH":
            return _Resp(200, {"html_url": "https://github.com/o/r/releases/tag/" + TAG,
                               "draft": False})
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def exe(tmp_path, monkeypatch):
    f = tmp_path / "AgentHub.exe"
    f.write_bytes(b"x" * 4096)
    monkeypatch.setattr(publish, "current_version", lambda: "9.9.9")
    monkeypatch.setattr(publish, "changelog_notes", lambda v: "notes")
    monkeypatch.setattr(publish.release_token, "resolve_token",
                        lambda p: {"token": "t", "source": "test"})
    monkeypatch.setattr(publish.ops, "publish_provenance",
                        lambda *a, **k: {"ok": True, "lines": [], "problems": []})
    monkeypatch.setattr(publish, "NET_BACKOFF", 0.0)
    monkeypatch.delenv("AGENTHUB_UPLOAD_RETRIES", raising=False)
    return f


def _run(monkeypatch, fake, exe):
    monkeypatch.setattr(publish, "_opener", lambda token: fake)
    return publish.main(["--yes", "--repo", "o/r", "--exe", str(exe)])


def test_interrupted_upload_retries_then_succeeds(exe, monkeypatch, capsys):
    fake = Opener([NET, ("ok", None)], [[]])
    assert _run(monkeypatch, fake, exe) == 0
    assert fake.count("POST", "https://uploads.github.com") == 2
    assert fake.count("PATCH") == 1
    out = capsys.readouterr().out
    assert "上传中断（第 1/2 次）" in out
    assert "重传" in out and "exe 上传：uploaded" in out


def test_lost_response_reconciled_by_asset_table(exe, monkeypatch, capsys):
    """附件其实落库了、响应丢了：不许删掉重传，按附件表对账收工。"""
    fake = Opener([NET], [[], [_asset]])   # 第一次查询是上传前的同名旧附件检查
    assert _run(monkeypatch, fake, exe) == 0
    assert fake.count("POST", "https://uploads.github.com") == 1, "对上了就不该再传"
    assert fake.count("DELETE") == 0, "别把刚传好的附件删了"
    assert fake.count("PATCH") == 1
    out = capsys.readouterr().out
    assert "大小一致" in out and "对账" in out


def test_size_mismatch_asset_deleted_then_reuploaded(exe, monkeypatch, capsys):
    fake = Opener([HTTP503, ("ok", None)],
                  [[], [dict(_asset, size=100)]])
    assert _run(monkeypatch, fake, exe) == 0
    assert fake.count("DELETE") == 1
    assert fake.count("POST", "https://uploads.github.com") == 2
    out = capsys.readouterr().out
    assert "大小 100" in out and "已删除" in out


def test_all_attempts_fail_keeps_draft_and_rc1(exe, monkeypatch, capsys):
    fake = Opener([HTTP503, HTTP503], [[], []])
    assert _run(monkeypatch, fake, exe) == 1
    assert fake.count("POST", "https://uploads.github.com") == 2
    assert fake.count("PATCH") == 0
    out = capsys.readouterr().out
    assert "共 2 次尝试" in out
    assert "仍是草稿" in out and "--upload-only" in out


def test_upload_retries_env_bounds(monkeypatch):
    monkeypatch.delenv("AGENTHUB_UPLOAD_RETRIES", raising=False)
    assert publish._upload_retries() == 2
    monkeypatch.setenv("AGENTHUB_UPLOAD_RETRIES", "4")
    assert publish._upload_retries() == 4
    monkeypatch.setenv("AGENTHUB_UPLOAD_RETRIES", "abc")
    assert publish._upload_retries() == 2
    monkeypatch.setenv("AGENTHUB_UPLOAD_RETRIES", "0")
    assert publish._upload_retries() == 1
    monkeypatch.setenv("AGENTHUB_UPLOAD_RETRIES", "99")
    assert publish._upload_retries() == 5
