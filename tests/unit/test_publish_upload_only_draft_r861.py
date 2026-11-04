# -*- coding: utf-8 -*-
"""R861（B075）：`--upload-only` 必须能看见草稿。

现场：v2.41.212 的 exe 两次撞 `WinError 10060`，Release 停在草稿、附件为空；
脚本自己打印的恢复命令是 `--upload-only`，而这条路径用 `GET /releases/tags/<tag>`
查版本——真 GitHub 对草稿一律 404，于是它回答「版本不存在，无从补传」，把唯一的
恢复入口堵死。R860 只在 `--publish-only` 里修过这个 404，补传入口漏了。

三条不变量钉在这里：
  ① 草稿从列表（/releases?per_page=100）捞到，不许再问 by-tag 端点；
  ② 真的没有 Release 时才报「不存在」；
  ③ 不带 --upload-only 时，已有草稿必须拒绝重复创建（否则同一个 tag 建出第二条）。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import publish  # noqa: E402

TAG = "v2.41.212"
ASSET = f"AgentHub-{TAG}.exe"


class _FakeGH:
    """按真 GitHub 的脾气应答：列表里有草稿，by-tag 端点看不见草稿。"""

    def __init__(self, release=None, published=False):
        self.release = release
        self.published = published
        self.calls = []

    def __call__(self, opener, url, method=None, data=None, timeout=120):
        self.calls.append((method or "GET", url, data))
        if "/releases?per_page=" in url:
            return 200, ([self.release] if self.release else []), {}
        if "/releases/tags/" in url:
            if self.release and not self.release.get("draft"):
                return 200, self.release, {}
            return 404, {"message": "Not Found"}, {}
        if url.endswith("/assets"):
            return 200, [], {}
        if method == "PATCH":
            return 200, {"html_url": f"https://github.com/o/r/releases/tag/{TAG}",
                         "draft": False}, {}
        raise AssertionError(f"意外请求：{method} {url}")

    def urls(self):
        return [u for _m, u, _d in self.calls]


def _rel(draft=True):
    return {"id": 433643942, "tag_name": TAG, "draft": draft,
            "html_url": f"https://github.com/o/r/releases/tag/{TAG}",
            "upload_url": "https://uploads.github.com/u{?name,label}"}


def _wire(monkeypatch, api, uploaded):
    monkeypatch.setattr(publish, "current_version", lambda: "2.41.212")
    monkeypatch.setattr(publish, "changelog_notes", lambda v: "notes")
    monkeypatch.setattr(publish.release_token, "resolve_token",
                        lambda *_a, **_k: {"token": "ghp_fake", "source": "测试",
                                           "tried": []})
    monkeypatch.setattr(publish, "preflight",
                        lambda opener, repo: {"ok": True, "login": "tester",
                                              "scopes": "repo", "problems": []})
    monkeypatch.setattr(publish.ops, "publish_provenance",
                        lambda *a, **k: {"ok": True, "lines": [], "problems": []})
    monkeypatch.setattr(publish.ops, "_file_hash", lambda path, algo: "0" * 64)
    monkeypatch.setattr(publish, "_reset_channel", lambda: None)
    monkeypatch.setattr(publish, "_opener", lambda token: object())
    monkeypatch.setattr(publish, "_channel_label", lambda opener: "测试通道")
    monkeypatch.setattr(publish, "_upload_timeout", lambda: 5.0)
    monkeypatch.setattr(publish, "_call_retry", api)

    def fake_upload(opener, url, base, rel_id, asset_name, exe, timeout):
        uploaded.append((rel_id, asset_name, url))
        return {"state": "uploaded", "browser_download_url": "https://dl/a.exe"}, None

    monkeypatch.setattr(publish, "_upload_asset", fake_upload)


@pytest.fixture
def exe(tmp_path):
    f = tmp_path / "AgentHub.exe"
    f.write_bytes(b"x" * 4096)
    return f


def _run(exe, extra=()):
    return publish.main(["--yes", "--repo", "o/r", "--exe", str(exe), *extra])


def test_upload_only_finds_draft_through_list(exe, monkeypatch, capsys):
    api = _FakeGH(release=_rel(draft=True))
    uploaded = []
    _wire(monkeypatch, api, uploaded)
    assert _run(exe, ("--upload-only",)) == 0
    assert any("/releases?per_page=" in u for u in api.urls()), api.calls
    assert not any("/releases/tags/" in u for u in api.urls()), \
        "草稿已在列表里找到，不该再问看不见草稿的 by-tag 端点"
    assert uploaded == [(433643942, ASSET, "https://uploads.github.com/u?name=" + ASSET)]
    assert [d for m, _u, d in api.calls if m == "PATCH"] == [{"draft": False}]
    assert "Release 已公开" in capsys.readouterr().out


def test_upload_only_reports_missing_release(exe, monkeypatch):
    api = _FakeGH(release=None)
    uploaded = []
    _wire(monkeypatch, api, uploaded)
    with pytest.raises(SystemExit) as e:
        _run(exe, ("--upload-only",))
    assert "不存在" in str(e.value)
    assert uploaded == []


def test_plain_publish_refuses_to_create_second_release_for_existing_draft(exe, monkeypatch):
    api = _FakeGH(release=_rel(draft=True))
    uploaded = []
    _wire(monkeypatch, api, uploaded)
    with pytest.raises(SystemExit) as e:
        _run(exe)
    msg = str(e.value)
    assert "已存在" in msg and "--upload-only" in msg
    assert not any(m == "POST" for m, _u, _d in api.calls), "不许再建一条同 tag Release"


def test_published_release_still_reachable_via_tag_fallback(exe, monkeypatch, capsys):
    """列表只翻 100 条：老版本可能捞不到，退回 by-tag 端点仍要能用。"""
    api = _FakeGH(release=_rel(draft=False))
    uploaded = []
    _wire(monkeypatch, api, uploaded)

    def list_misses(opener, url, method=None, data=None, timeout=120):
        if "/releases?per_page=" in url:
            api.calls.append(("GET", url, None))
            return 200, [], {}
        return api(opener, url, method, data, timeout)

    monkeypatch.setattr(publish, "_call_retry", list_misses)
    assert _run(exe, ("--upload-only",)) == 0
    assert any("/releases/tags/" in u for u in api.urls())
    assert not [d for m, _u, d in api.calls if m == "PATCH"], "已公开版本不该再 PATCH"
