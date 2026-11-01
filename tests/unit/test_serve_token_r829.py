# -*- coding: utf-8 -*-
"""R829b：`serve --allow-write` 必须把 token 打印出来，而且要能被机器读走。

定罪：R757（commit 7be8636）把启动横幅重写成 `_serve_banner` 时，删掉了原来的
`token={token}`，新文案却写「token 见上一行」——上一行是纯 URL，没有 token。
两条后果：①用户按提示找不到 token，写请求永远 403，`--allow-write` 形同虚设；
②`adv_cli::t_serve_subprocess` 在主线程 `readline()` 等一行永远不会来的输出，
没有硬超时 → 整条 adv 链停在 adv_cli 上挂死（2026-11-01 实测三条链都卡在这）。
所以「token=」这句文案不是修辞，是被测试和用户脚本共同依赖的接口。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402

# adv 侧用的就是这条正则，口径必须一致
TOKEN_RE = r"token=([0-9a-fA-F]+)"


class _FakeHttpd:
    """把 serve_forever 的阻塞段短路掉：本用例只验启动输出，不验 HTTP。"""

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        pass


def _startup_text(monkeypatch, allow_write: bool, token: str) -> str:
    import io
    from contextlib import redirect_stdout

    monkeypatch.setattr(api, "serve",
                        lambda root, port, aw, ob: (_FakeHttpd(), 8787, token))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = api.serve_forever("根", 8787, allow_write, False)
    assert rc == 0, rc
    return buf.getvalue()


def test_allow_write_prints_parseable_token(monkeypatch):
    out = _startup_text(monkeypatch, True, "deadbeef90ab8877")
    m = re.search(TOKEN_RE, out)
    assert m, f"--allow-write 的启动输出里没有可解析的 token：{out!r}"
    assert m.group(1) == "deadbeef90ab8877", m.group(1)


def test_readonly_prints_no_token(monkeypatch):
    """只读默认不该凭空造出一个 token（否则用户以为带着就能写）。"""
    out = _startup_text(monkeypatch, False, "")
    assert not re.search(TOKEN_RE, out), out
    assert "已关闭" in out and "--allow-write" in out, out


def test_banner_does_not_point_at_a_line_that_has_no_token():
    """「token 见上一行」这类指引必须自证：上一行真含 token 才许这么说。"""
    lines = api._serve_banner(8787, True, "aabbccddeeff0011")
    joined = "\n".join(lines)
    assert re.search(TOKEN_RE, joined), joined
    assert "见上一行" not in joined or re.search(TOKEN_RE, joined.split("见上一行")[0]), joined
