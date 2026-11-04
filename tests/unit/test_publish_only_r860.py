# -*- coding: utf-8 -*-
"""R860（B069）：publish.py --publish-only 的契约。

由来：v2.41.209 的草稿已经带着 102MB 的 exe 躺在 GitHub 上，公开它只需要一次
`PATCH draft=false`，但脚本只有「建 Release/上传」这一条路——要么重跑 --upload-only
白传 102MB，要么写一次性脚本绕 API。缺的是入口不是能力。

两条不变量在这里钉死：
  ① 附件为空的草稿**拒绝公开**（v2.41.159 / v2.41.77 那种「公开页上挂个没 exe 的版本」）；
  ② 已公开的版本再跑一次必须非零退出（幂等，但不是静默成功）。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import publish  # noqa: E402


class _FakeAPI:
    def __init__(self, release=None, assets=None, patch_status=200, patch_draft=False):
        self.release = release
        self.assets = assets if assets is not None else []
        self.patch_status = patch_status
        self.patch_draft = patch_draft
        self.calls = []

    def __call__(self, opener, url, method=None, data=None, timeout=120):
        self.calls.append((method or "GET", url, data))
        if "/releases?per_page=" in url:
            return 200, ([self.release] if self.release else []), {}
        if url.endswith("/assets"):
            return 200, self.assets, {}
        if method == "PATCH":
            body = {"html_url": "https://github.com/o/r/releases/tag/v9", "draft": self.patch_draft}
            return self.patch_status, body, {}
        if "/releases/tags/" in url:
            # 真 GitHub 的 by-tag 端点对草稿一律 404（2026-11-04 实测）
            if self.release is None or self.release.get("draft"):
                return 404, {"message": "Not Found"}, {}
            return 200, self.release, {}
        raise AssertionError(f"意外请求：{url}")


def _wire(monkeypatch, api, version="2.41.211"):
    monkeypatch.setattr(publish, "current_version", lambda: version)
    monkeypatch.setattr(publish.release_token, "resolve_token",
                        lambda *_a, **_k: {"token": "ghp_fake", "source": "测试", "tried": []})
    monkeypatch.setattr(publish, "_reset_channel", lambda: None)
    monkeypatch.setattr(publish, "_opener", lambda token: object())
    monkeypatch.setattr(publish, "_channel_label", lambda opener: "测试通道")
    monkeypatch.setattr(publish, "preflight",
                        lambda opener, repo: {"ok": True, "login": "tester", "scopes": "repo",
                                              "problems": []})
    monkeypatch.setattr(publish, "_call_retry", api)


def _draft(assets, tag="v2.41.211"):
    return {"id": 42, "tag_name": tag, "draft": True,
            "html_url": "https://github.com/o/r/releases/tag/v9",
            "upload_url": "https://u/{?name}", "assets": assets}


def _asset(tag, size=102261450):
    return {"name": f"AgentHub-{tag}.exe", "size": size, "state": "uploaded"}


def test_preview_makes_no_request(monkeypatch):
    api = _FakeAPI(release=_draft([]))
    _wire(monkeypatch, api)
    assert publish.main(["--publish-only"]) == 0
    assert api.calls == []


def test_publishes_draft_with_nonempty_asset(monkeypatch, capsys):
    api = _FakeAPI(release=_draft([_asset("v2.41.211")]), assets=[_asset("v2.41.211")])
    _wire(monkeypatch, api)
    assert publish.main(["--publish-only", "--yes"]) == 0
    patch = [c for c in api.calls if c[0] == "PATCH"]
    assert len(patch) == 1, api.calls
    assert patch[0][2] == {"draft": False}
    out = capsys.readouterr().out
    assert "Release 已公开" in out
    assert "102261450" in out


def test_refuses_empty_attachment_and_never_patches(monkeypatch, capsys):
    api = _FakeAPI(release=_draft([]), assets=[])
    _wire(monkeypatch, api)
    with pytest.raises(SystemExit) as e:
        publish.main(["--publish-only", "--yes"])
    assert "空附件" in str(e.value)
    assert not [c for c in api.calls if c[0] == "PATCH"]


def test_already_published_is_nonzero_idempotent_error(monkeypatch):
    rel = _draft([_asset("v2.41.211")])
    rel["draft"] = False
    api = _FakeAPI(release=rel, assets=[_asset("v2.41.211")])
    _wire(monkeypatch, api)
    with pytest.raises(SystemExit) as e:
        publish.main(["--publish-only", "--yes"])
    assert "已经公开" in str(e.value)
    assert not [c for c in api.calls if c[0] == "PATCH"]


def test_missing_draft_points_back_to_normal_publish(monkeypatch):
    api = _FakeAPI(release=None)
    _wire(monkeypatch, api)
    with pytest.raises(SystemExit) as e:
        publish.main(["--publish-only", "--yes"])
    assert "无从公开" in str(e.value)


def test_tag_option_targets_old_version(monkeypatch, capsys):
    api = _FakeAPI(release=_draft([_asset("v2.41.209")], tag="v2.41.209"),
                   assets=[_asset("v2.41.209")])
    _wire(monkeypatch, api, version="2.41.211")
    assert publish.main(["--publish-only", "--tag", "v2.41.209", "--yes"]) == 0
    assert any("/releases?per_page=" in c[1] for c in api.calls), api.calls
    assert "v2.41.209（--tag 指定）" in capsys.readouterr().out


def test_draft_is_found_through_list_because_tags_endpoint_hides_it(monkeypatch, capsys):
    """真 GitHub 的 /releases/tags/<tag> 对草稿返回 404——第一版就栽在这里。

    假 API 若让 by-tag 端点也返回草稿，这个缺陷永远测不出来；所以这里把真行为钉进
    假实现，并断言草稿是从列表（/releases?per_page=100）捞到的。
    """
    api = _FakeAPI(release=_draft([_asset("v2.41.211")]), assets=[_asset("v2.41.211")])
    _wire(monkeypatch, api)
    assert publish.main(["--publish-only", "--yes"]) == 0
    list_calls = [c for c in api.calls if "/releases?per_page=" in c[1]]
    assert list_calls, api.calls
    tags_calls = [c for c in api.calls if "/releases/tags/" in c[1]]
    assert not tags_calls, "草稿已在列表里找到，不该再问 by-tag 端点"
    assert "Release 已公开" in capsys.readouterr().out


def test_falls_back_to_tags_endpoint_when_list_misses(monkeypatch):
    """列表分页 100 条：老版本可能已翻出第一页，退回 by-tag 端点。"""
    rel = _draft([_asset("v2.41.211")])
    rel["draft"] = False   # 假 API 的 by-tag 端点只对已公开版本返回 200（与真行为一致）
    api = _FakeAPI(release=rel, assets=[_asset("v2.41.211")])
    _wire(monkeypatch, api)
    real_call = api.__call__

    def call(opener, url, method=None, data=None, timeout=120):
        if "/releases?per_page=" in url:
            api.calls.append(("GET", url, None))
            return 200, [], {}
        return real_call(opener, url, method, data, timeout)

    monkeypatch.setattr(publish, "_call_retry", call)
    assert publish._find_release(object(), "https://api.github.com/repos/o/r", "v2.41.211")[0] == 200


def test_mutually_exclusive_with_upload_only(monkeypatch):
    api = _FakeAPI(release=_draft([_asset("v2.41.211")]))
    _wire(monkeypatch, api)
    with pytest.raises(SystemExit) as e:
        publish.main(["--publish-only", "--upload-only", "--yes"])
    assert "互斥" in str(e.value)
    assert api.calls == []


def test_patch_failure_keeps_draft_and_returns_1(monkeypatch, capsys):
    api = _FakeAPI(release=_draft([_asset("v2.41.211")]), assets=[_asset("v2.41.211")],
                   patch_status=502, patch_draft=True)
    _wire(monkeypatch, api)
    assert publish.main(["--publish-only", "--yes"]) == 1
    assert "仍是草稿" in capsys.readouterr().out
