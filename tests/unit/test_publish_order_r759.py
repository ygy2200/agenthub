# -*- coding: utf-8 -*-
r"""R759：Release 的 tag 落在「发布那一刻的 main」上——所以必须先推再发。

实测（`git ls-remote --tags origin` + `git show`）：
    v2.41.159 -> 8eda8b7（R731–R734 的提交，不是 R740 那个 exe 的源码）
    v2.41.160 -> 6e434e9（R740 的 bump 提交，而 exe 来自 R741/R742）
GitHub 在 tag 不存在时按**创建 Release 时的 main** 建 tag；原来的顺序是
`publish.py --yes` 之后再 `git push`，于是新 tag 永远指向上一批提交。

口径：`--push` 必须在任何 GitHub 写入之前完成；推失败就停（远端零改动）。
入口脚本 release.bat 也必须真的调用它（带日志文件），否则等于没有。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def test_push_failure_leaves_no_release(tmp_path, monkeypatch):
    import publish

    calls = []

    class _NeverCalled:
        def open(self, req, timeout=None):
            calls.append(req.full_url)
            raise AssertionError("推失败后不许再碰 GitHub")

    exe = tmp_path / "AgentHub.exe"
    exe.write_bytes(b"x" * 1024)
    monkeypatch.setattr(publish, "current_version", lambda: "9.9.9")
    monkeypatch.setattr(publish, "changelog_notes", lambda v: "notes")
    monkeypatch.setattr(publish.release_token, "resolve_token",
                        lambda p: {"token": "t", "source": "test"})
    monkeypatch.setattr(publish.ops, "publish_provenance",
                        lambda *a, **k: {"ok": True, "lines": [], "problems": []})
    # 预检本身要过，才能走到 push 分支
    monkeypatch.setattr(publish, "preflight",
                        lambda opener, repo: {"ok": True, "login": "me", "scopes": "",
                                              "problems": []})
    monkeypatch.setattr(publish, "_opener", lambda token: _NeverCalled())
    monkeypatch.setattr(publish, "_git_push", lambda: 1)

    rc = publish.main(["--yes", "--repo", "o/r", "--exe", str(exe), "--push"])
    assert rc == 1
    assert calls == [], "推失败后不许建 Release"


def test_release_bat_publishes_with_log_and_push():
    text = (ROOT / "release.bat").read_text(encoding="utf-8", errors="replace")
    assert "publish.py" in text, "一键发布必须包含发布会话"
    line = next(l for l in text.splitlines() if "publish.py" in l and "--yes" in l)
    assert "--push" in line, "必须在建 Release 前推代码（tag 钉在当时的 main 上）"
    assert "--log-file" in line, "失败时日志文件是唯一证据（管道会吃退出码）"
    # 失败分支要指向日志与补传入口
    assert re.search(r"if errorlevel 1 \(\s*\n.*publish failed", text, re.I)


def test_release_bat_stays_ascii():
    """cmd 的 OEM 码页会把非 ASCII 执行行打成乱码再当命令跑（R720 的坑）。"""
    raw = (ROOT / "release.bat").read_bytes()
    assert all(b < 128 for b in raw), "release.bat 必须是纯 ASCII"
