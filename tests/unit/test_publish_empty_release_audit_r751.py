# -*- coding: utf-8 -*-
"""R751：--check 只验 token 与仓库权限，看不见「公开页上挂着空附件版本」。

R747 清掉的 v2.41.159 / v2.41.77 是人手翻 Releases 页才发现的——发布工具自己
能看见的信息，不该指望人去翻。口径：草稿不算问题（访客看不见），其余空附件要点名。
"""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import publish  # noqa: E402


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._raw = json.dumps(payload).encode()
        self.headers = {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, payload, status=200, boom=False):
        self.payload, self.status, self.boom = payload, status, boom

    def open(self, req, timeout=None):
        if self.boom:
            raise publish.urllib.error.URLError("boom")
        return _Resp(self.status, self.payload)


def _rel(tag, assets=1, draft=False):
    return {"tag_name": tag, "draft": draft,
            "assets": [{"name": "a.exe"}] if assets else []}


def test_no_empty_releases():
    out = publish._empty_release_audit(FakeOpener([_rel("v1", 1), _rel("v2", 2)]), "o/r")
    assert out == {"ok": True, "error": "", "tags": []}


def test_empty_release_is_named_and_drafts_ignored():
    payload = [_rel("v3", 0), _rel("v4", 0, draft=True), _rel("v5", 1)]
    out = publish._empty_release_audit(FakeOpener(payload), "o/r")
    assert out["ok"] and out["tags"] == ["v3"], "草稿不可见，不该算公开页问题"


def test_api_failure_is_reported_not_swallowed(monkeypatch):
    assert publish._empty_release_audit(FakeOpener({}, status=500), "o/r")["ok"] is False
    monkeypatch.setattr(publish, "NET_BACKOFF", 0)
    out = publish._empty_release_audit(FakeOpener({}, boom=True), "o/r")
    assert out["ok"] is False and "boom" in out["error"]


def test_audit_is_wired_into_check(monkeypatch, capsys):
    """--check 的输出里必须真出现体检行（否则函数写了但没人调用）。"""
    monkeypatch.setattr(publish, "current_version", lambda: "9.9.9")
    monkeypatch.setattr(publish, "changelog_notes", lambda v: "notes")
    monkeypatch.setattr(publish.release_token, "resolve_token",
                        lambda p: {"token": "t", "source": "test"})
    monkeypatch.setattr(publish.ops, "publish_provenance",
                        lambda *a, **k: {"ok": True, "lines": [], "problems": []})

    class Op(FakeOpener):
        def open(self, req, timeout=None):
            url = req.full_url
            if url.endswith("/user"):
                return _Resp(200, {"login": "me"})
            if url.endswith("/repos/o/r"):
                return _Resp(200, {"permissions": {"push": True}})
            return _Resp(200, [_rel("v9", 0)])

    monkeypatch.setattr(publish, "_opener", lambda t: Op({}))
    assert publish.main(["--check", "--repo", "o/r"]) == 0
    out = capsys.readouterr().out
    assert "Release 体检" in out and "v9" in out
