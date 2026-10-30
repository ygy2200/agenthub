# -*- coding: utf-8 -*-
r"""R761：建 Release 撞上网络抖动时，既不许重复建，也不许裸抛 traceback。

真实事故（2026-10-30 发 v2.41.161）：`publish.py --yes --push` 的 POST /releases
撞上 `SSL: UNEXPECTED_EOF_WHILE_READING`，进程直接抛栈退出。事后查 /releases/tags
是 404（远端什么都没写），但屏幕上只有一段 Python 栈——人会以为「发布流程坏了」。

口径：
  ① POST 抛 NetworkError 后先 GET 同 tag：建出来了就沿用 id（幂等恢复），绝不重复建；
  ② GET 也 404 才重试，重试次数有上限（NET_RETRIES），失败后走顶层收口；
  ③ GitHub 回 422（tag 已存在）同样去取回已有 Release；
  ④ 顶层 NetworkError 返回 1，打印可操作的说明（代理写法/代码推没推/怎么重跑），
     不出现 traceback。
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import publish  # noqa: E402

REL = "https://api.github.com/repos/o/r/releases"


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


def _release(rid=77):
    return {"id": rid, "draft": True,
            "html_url": f"https://github.com/o/r/releases/tag/v9.9.9",
            "upload_url": "https://uploads.github.com/u{?name,label}"}


NET = "net"  # 剧本记号：这一步抛 NetworkError


class Opener:
    """按剧本应答。tags_plan / post_plan 依次消费，用完按默认值兜底。"""

    def __init__(self, tags_plan, post_plan):
        self.tags_plan = list(tags_plan)
        self.post_plan = list(post_plan)
        self.calls = []

    def count(self, method, suffix=""):
        return sum(1 for m, u in self.calls
                   if m == method and u.startswith(REL) and u.endswith(suffix))

    def open(self, req, timeout=None):
        url = req.full_url
        method = req.get_method().upper()
        self.calls.append((method, url))
        if url.endswith("/user"):
            return _Resp(200, {"login": "me"}, {"X-OAuth-Scopes": "repo"})
        if re.search(r"/repos/o/r$", url):
            return _Resp(200, {"permissions": {"push": True}})
        if "/releases/tags/" in url:
            step = self.tags_plan.pop(0) if self.tags_plan else (404, {"message": "Not Found"})
            if step == NET:
                raise publish.NetworkError("SSL: UNEXPECTED_EOF_WHILE_READING")
            return _Resp(step[0], step[1])
        if method == "POST" and url.endswith("/releases"):
            step = self.post_plan.pop(0) if self.post_plan else NET
            if step == NET:
                raise publish.NetworkError("SSL: UNEXPECTED_EOF_WHILE_READING")
            return _Resp(step[0], step[1])
        if re.search(r"/releases/\d+/assets$", url):
            return _Resp(200, [])
        if url.startswith("https://uploads.github.com"):
            return _Resp(201, {"state": "uploaded",
                               "browser_download_url": "https://dl/a.exe"})
        if method == "PATCH":
            return _Resp(200, {"html_url": "https://github.com/o/r/releases/tag/v9.9.9",
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
    return f


def _run(monkeypatch, fake, exe, extra=()):
    monkeypatch.setattr(publish, "_opener", lambda token: fake)
    return publish.main(["--yes", "--repo", "o/r", "--exe", str(exe), *extra])


def test_network_error_then_get_found_reuses_release(exe, monkeypatch, capsys):
    """POST 响应丢了但 Release 其实建成了：必须沿用，不能再 POST 一次。"""
    fake = Opener([(404, {"message": "Not Found"}), (200, _release())], [NET])
    assert _run(monkeypatch, fake, exe) == 0
    assert fake.count("POST", "/releases") == 1, "建成的那次不该被重做"
    assert fake.count("PATCH") == 1, "沿用后应继续走完上传与公开"
    out = capsys.readouterr().out
    assert "已经建好" in out and "id=77" in out


def test_network_error_then_404_retries_bounded_and_exits_1(exe, monkeypatch, capsys):
    """真没建成：重试有上限，最后干净退出，不裸抛 traceback。"""
    fake = Opener([(404, {})] + [(404, {})] * publish.NET_RETRIES,
                  [NET] * publish.NET_RETRIES)
    assert _run(monkeypatch, fake, exe) == 1
    assert fake.count("POST", "/releases") == publish.NET_RETRIES
    assert fake.count("PATCH") == 0, "没建成就无所谓公开"
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert "连不上 GitHub API" in out
    assert "HTTPS_PROXY=http://127.0.0.1:7890" in out
    assert "重跑同一条命令" in out and "空附件版本" in out


def test_422_fetches_existing_release(exe, monkeypatch, capsys):
    """POST 被 422 拒绝（tag 已存在）：取回已有 Release 继续，不当失败。"""
    fake = Opener([(404, {}), (200, _release(88))],
                  [(422, {"message": "Validation Failed"})])
    assert _run(monkeypatch, fake, exe) == 0
    out = capsys.readouterr().out
    assert "已存在，沿用 id=88" in out
    assert fake.count("PATCH") == 1


def test_top_level_network_error_reports_push_and_log(exe, monkeypatch, capsys, tmp_path):
    """顶层收口：说清代码已推送、日志在哪，且日志模式照常还原 stdout。"""
    log = tmp_path / "publish.log"
    monkeypatch.setattr(publish, "_git_push", lambda: 0)
    fake = Opener([(404, {})] + [(404, {})] * publish.NET_RETRIES,
                  [NET] * publish.NET_RETRIES)
    old_out = sys.stdout
    assert _run(monkeypatch, fake, exe, ("--push", "--log-file", str(log))) == 1
    assert sys.stdout is old_out
    out = capsys.readouterr().out
    assert "代码推送：已推送" in out
    assert f"完整日志：{log}" in out
    text = log.read_text(encoding="utf-8")
    assert "连不上 GitHub API" in text and "HTTPS_PROXY" in text
