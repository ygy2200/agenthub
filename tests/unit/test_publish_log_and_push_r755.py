# -*- coding: utf-8 -*-
r"""R755：发布脚本的外层包装——管道会吃掉退出码。

真实事故（2026-10-29 发 v2.41.160）：`python publish.py --yes --push | tail -2`
里 tail 的退出码顶掉了 publish 的，推送失败时我只看到「已上传」两行，
要靠事后 `git status` 才发现没推上去。同一天还踩过
`git push … | tail -2 && break` 永远为真的坑。

口径：
  ① `--log-file` 把所有输出同时落盘（屏幕照旧），失败时日志文件是唯一证据；
  ② `_Tee` 单流坏掉不许中断发布；
  ③ 日志模式必须还原 sys.stdout/stderr（pytest 同进程调用否则写进已关闭文件）；
  ④ `--push` 只在发布链路真正走完（返回 0）后执行，且把 git 的非零原样传出。
"""
import io
import json
import re
import sys
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
    def __init__(self, upload_ok=True):
        self.upload_ok = upload_ok
        self.calls = []

    def open(self, req, timeout=None):
        import urllib.error

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
            return _Resp(404, {"message": "Not Found"})
        if method == "POST" and url.endswith("/releases"):
            return _Resp(201, {"id": 42, "draft": True, "html_url": "https://x/untagged",
                               "upload_url": "https://uploads.github.com/u{?name,label}"})
        if re.search(r"/releases/\d+/assets$", url):
            return _Resp(200, [])
        if url.startswith("https://uploads.github.com"):
            if not self.upload_ok:
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", {},
                                             io.BytesIO(b"{}"))
            return _Resp(201, {"state": "uploaded", "browser_download_url": "https://dl/a.exe"})
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
    return f


# ---------- _Tee 本体 ----------

def test_tee_writes_all_streams():
    a, b = io.StringIO(), io.StringIO()
    tee = publish._Tee(a, b)
    tee.write("hello")
    tee.flush()
    assert a.getvalue() == "hello" and b.getvalue() == "hello"


def test_tee_survives_broken_stream():
    """写日志失败（磁盘满/句柄关）不能把发布带崩，屏幕那份还得写完。"""
    class Broken:
        def write(self, text):
            raise OSError("disk full")

        def flush(self):
            raise OSError("disk full")

    good = io.StringIO()
    tee = publish._Tee(Broken(), good)
    assert tee.write("still here") == len("still here")
    tee.flush()
    assert good.getvalue() == "still here"


def test_tee_reports_encoding_of_first_real_stream():
    assert publish._Tee(io.StringIO(), io.StringIO()).encoding
    assert publish._Tee(object()).encoding  # 兜底 utf-8，不抛


# ---------- --log-file ----------

def test_log_file_mirrors_screen_and_keeps_streams(exe, monkeypatch, tmp_path, capsys):
    log = tmp_path / "publish.log"
    monkeypatch.setattr(publish, "_opener", lambda token: FakeOpener())
    old_out, old_err = sys.stdout, sys.stderr
    rc = publish.main(["--repo", "o/r", "--exe", str(exe), "--log-file", str(log)])
    assert rc == 0
    assert sys.stdout is old_out and sys.stderr is old_err, "必须还原，否则同进程后续输出全被劫持"

    text = log.read_text(encoding="utf-8")
    screen = capsys.readouterr().out
    assert "日志：" in text and "仓库：o/r" in text
    assert "仓库：o/r" in screen, "屏幕输出不能被日志模式吞掉"
    assert "预演结束" in text


def test_log_file_closed_after_run(exe, monkeypatch, tmp_path):
    log = tmp_path / "publish.log"
    monkeypatch.setattr(publish, "_opener", lambda token: FakeOpener())
    publish.main(["--repo", "o/r", "--exe", str(exe), "--log-file", str(log)])
    # 句柄没关的话，Windows 上独占删不掉
    log.unlink()
    assert not log.exists()


def test_streams_restored_even_if_run_raises(monkeypatch, tmp_path):
    log = tmp_path / "publish.log"
    old_out, old_err = sys.stdout, sys.stderr

    def boom(_args):
        raise RuntimeError("炸了")

    monkeypatch.setattr(publish, "_run", boom)
    with pytest.raises(RuntimeError):
        publish.main(["--log-file", str(log)])
    assert sys.stdout is old_out and sys.stderr is old_err
    log.unlink()  # 同样验证句柄已关


# ---------- --push ----------

def _published(monkeypatch, fake, exe, extra=()):
    monkeypatch.setattr(publish, "_opener", lambda token: fake)
    return publish.main(["--yes", "--repo", "o/r", "--exe", str(exe), *extra])


def test_push_returns_git_rc(exe, monkeypatch, capsys):
    """push 失败必须变成 publish 的非零，不能被包成成功。"""
    calls = []
    monkeypatch.setattr(publish, "_git_push", lambda: (calls.append(1), 1)[1])
    assert _published(monkeypatch, FakeOpener(), exe, ("--push",)) == 1
    assert calls == [1]


def test_push_runs_after_successful_publish(exe, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(publish, "_git_push", lambda: (calls.append(1), 0)[1])
    assert _published(monkeypatch, FakeOpener(), exe, ("--push",)) == 0
    assert calls == [1], "发布成功后才 push"


def test_failed_upload_never_pushes(exe, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(publish, "_git_push", lambda: (calls.append(1), 0)[1])
    assert _published(monkeypatch, FakeOpener(upload_ok=False), exe, ("--push",)) == 1
    assert calls == [], "附件都没传上，绝不能再报「已推送」混淆事实"


# ---------- _git_push 本身 ----------

class _Popen:
    def __init__(self, rc, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def test_git_push_rc_zero(monkeypatch, capsys):
    monkeypatch.setattr(publish.subprocess, "run", lambda *a, **k: _Popen(0, "ok"))
    assert publish._git_push() == 0
    assert "已推送" in capsys.readouterr().out


def test_git_push_failure_is_loud(monkeypatch, capsys):
    monkeypatch.setattr(publish.subprocess, "run", lambda *a, **k: _Popen(128, "", "rejected"))
    assert publish._git_push() == 1
    out = capsys.readouterr().out
    assert "rejected" in out and "RC=128" in out and "重跑 git push" in out
