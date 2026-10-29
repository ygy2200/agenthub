# -*- coding: utf-8 -*-
r"""R748：发布链路会留下「空附件版本」——先公开、后传附件。

实测（2026-10-29 的 v2.41.159）：`publish.py --yes` 上传超时，Release 已经建出来，
公开页上就挂着一个**没有 exe** 的版本；同一形态还有更早的 v2.41.77。
用户看到的是「发版没带 exe」，而不是「上传失败」——错误被状态掩盖了。

口径：Release 先建成草稿，exe 传成功后再 PATCH 转正式。
传失败就停在草稿（访客看不见），恢复命令 `--upload-only` 成功后会顺手转正式。
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


class FakeOpener:
    """按 URL 形状回放 GitHub API；upload_ok=False 时上传 503。"""

    def __init__(self, upload_ok=True, existing=None):
        self.upload_ok = upload_ok
        self.existing = existing
        self.calls = []

    def open(self, req, timeout=None):
        url = req.full_url
        method = req.get_method().upper()
        payload = None
        if method in ("POST", "PATCH") and not url.startswith("https://uploads.github.com"):
            payload = json.loads(req.data or b"{}")
        self.calls.append((method, url, payload))
        if url.endswith("/user"):
            return _Resp(200, {"login": "me"}, {"X-OAuth-Scopes": "repo"})
        if re.search(r"/repos/o/r$", url):
            return _Resp(200, {"permissions": {"push": True}})
        if "/releases/tags/" in url:
            return _Resp(200, self.existing) if self.existing else _Resp(404, {"message": "Not Found"})
        if method == "POST" and url.endswith("/releases"):
            return _Resp(201, {"id": 42, "draft": True, "html_url": "https://x/untagged",
                               "upload_url": "https://uploads.github.com/u{?name,label}"})
        if re.search(r"/releases/\d+/assets$", url):
            return _Resp(200, [])
        if url.startswith("https://uploads.github.com"):
            if not self.upload_ok:
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, io.BytesIO(b"{}"))
            return _Resp(201, {"state": "uploaded", "browser_download_url": "https://dl/a.exe"})
        if method == "PATCH":
            return _Resp(200, {"html_url": "https://github.com/o/r/releases/tag/v9.9.9", "draft": False})
        raise AssertionError(f"unexpected {method} {url}")

    def methods(self):
        return [(m, re.sub(r"\?.*$", "", u)) for m, u, _ in self.calls]


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
    return f


def _run(monkeypatch, fake, exe, extra=()):
    monkeypatch.setattr(publish, "_opener", lambda token: fake)
    return publish.main(["--yes", "--repo", "o/r", "--exe", str(exe), *extra])


def test_new_release_is_draft_then_published(exe, monkeypatch, capsys):
    fake = FakeOpener()
    assert _run(monkeypatch, fake, exe) == 0
    create = [p for m, u, p in fake.calls if m == "POST" and u.endswith("/releases")]
    assert create and create[0]["draft"] is True, "创建时必须带 draft=True"
    patch = [p for m, u, p in fake.calls if m == "PATCH"]
    assert patch == [{"draft": False}], "传完必须转正式"
    assert "已公开" in capsys.readouterr().out


def test_failed_upload_leaves_it_draft(exe, monkeypatch, capsys):
    fake = FakeOpener(upload_ok=False)
    assert _run(monkeypatch, fake, exe) == 1
    assert not [1 for m, u, p in fake.calls if m == "PATCH"], "没传上就不许公开"
    out = capsys.readouterr().out
    assert "仍是草稿" in out and "公开页看不到" in out
    assert "--upload-only" in out            # 给出恢复命令


def test_upload_only_publishes_a_draft(exe, monkeypatch, capsys):
    fake = FakeOpener(existing={"id": 7, "draft": True,
                                "upload_url": "https://uploads.github.com/v{?name,label}"})
    assert _run(monkeypatch, fake, exe, ("--upload-only",)) == 0
    assert [p for m, u, p in fake.calls if m == "PATCH"] == [{"draft": False}]
    assert "已公开" in capsys.readouterr().out


def test_upload_only_leaves_published_release_alone(exe, monkeypatch, capsys):
    """已公开的老版本补传附件时不该再 PATCH——避免多余的写操作与时间戳变动。"""
    fake = FakeOpener(existing={"id": 9, "draft": False,
                                "upload_url": "https://uploads.github.com/w{?name,label}"})
    assert _run(monkeypatch, fake, exe, ("--upload-only",)) == 0
    assert not [1 for m, u, p in fake.calls if m == "PATCH"]
    assert "已公开" not in capsys.readouterr().out
