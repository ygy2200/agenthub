# -*- coding: utf-8 -*-
r"""R743：serve 起不来说的是 OS 原话。

实测（8774 被占）：
    启动失败：[WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试。
——没说是「端口被占用」，没给换端口的下一步，用户只能去搜错误码。
10013 在 Windows 上常见于「端口已被占」或「防火墙拦」，这两件都可以自己绕过去。

口径：说清「端口 N 起不来」+ 最可能原因 + 两条可粘贴的下一步；
非端口类 OSError 不许硬说「被占用」（那就是误诊）。
"""
import errno
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402


def _win_err(code: int) -> OSError:
    e = PermissionError(13, "以一种访问权限不允许的方式做了一个访问套接字的尝试。")
    e.winerror = code
    return e


def test_taken_port_says_occupied_and_offers_next_port():
    text = api._port_error_text(_win_err(10013), 8776)
    assert "8776" in text
    assert "被别的程序占着" in text or "防火墙" in text
    assert "serve --port 8777" in text          # 下一步是「换端口」，可直接粘
    assert "netstat -ano | findstr :8776" in text


def test_eaddrinuse_on_posix_is_recognised():
    e = OSError(errno.EADDRINUSE, "Address already in use")
    text = api._port_error_text(e, 8787)
    assert "被别的程序占着" in text or "防火墙" in text


def test_unrelated_oserror_does_not_claim_occupied():
    e = OSError(errno.ENOENT, "No such file or directory")
    text = api._port_error_text(e, 8787)
    assert "被别的程序占着" not in text        # 不许误诊
    assert "serve --port 8788" in text          # 但下一步仍然给


def test_serve_forever_returns_1_with_hint(monkeypatch, capsys):
    def boom(*_a, **_k):
        raise _win_err(10013)

    monkeypatch.setattr(api, "serve", boom)
    rc = api.serve_forever("/tmp/fake-root", port=8791)
    out = capsys.readouterr().out
    assert rc == 1
    assert "8791" in out and "serve --port 8792" in out
