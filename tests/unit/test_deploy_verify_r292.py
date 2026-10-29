# -*- coding: utf-8 -*-
r"""部署后自校验（R292）。

R291 实测：源码 2.41.141，`~/.agenthub/mcp_server` 部署副本停在 2.41.119——
`deploy` 只打印「已部署」就返回 0，副本落后 22 个版本也无人察觉，接入方一直跑旧代码。
现在部署成功立刻跑 deploy_diff：不一致必须非零退出并给出细节。
"""
import cli
import core
import ops


def test_deploy_runs_diff_and_passes(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(core, "deploy_server", lambda: ("py", str(tmp_path / "srv")))
    seen = {}

    def fake_check(name, target, extra=""):
        seen["args"] = (name, target)
        return {"ok": True, "summary": "11/11 与部署一致（X）", "detail": []}

    monkeypatch.setattr(ops, "run_check", fake_check)
    rc = cli.main(["deploy", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert seen["args"][0] == "deploy_diff", seen
    assert "11/11 与部署一致" in out, out


def test_deploy_fails_when_diff_detected(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(core, "deploy_server", lambda: ("py", str(tmp_path / "srv")))
    monkeypatch.setattr(ops, "run_check", lambda *a, **k: {
        "ok": False,
        "summary": "10/11 与部署一致（X）",
        "detail": ["ops.py: 不一致（源码与部署谁新？先确认再同步）"],
    })
    rc = cli.main(["deploy", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "ops.py: 不一致" in out, out


def test_deploy_failure_skips_verification(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(core, "deploy_server", lambda: ("", ""))
    called = []

    def tripwire(*a, **k):
        called.append(1)
        return {"ok": True, "summary": "", "detail": []}

    monkeypatch.setattr(ops, "run_check", tripwire)
    rc = cli.main(["deploy", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert not called, "部署失败时不应再跑一致性校验"
    assert "部署失败" in out, out
