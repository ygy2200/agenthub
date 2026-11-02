# -*- coding: utf-8 -*-
r"""部署后自校验（R292）。

R291 实测：源码 2.41.141，`~/.agenthub/mcp_server` 部署副本停在 2.41.119——
`deploy` 只打印「已部署」就返回 0，副本落后 22 个版本也无人察觉，接入方一直跑旧代码。
现在部署成功立刻跑 deploy_diff：不一致必须非零退出并给出细节。
"""
import cli
import core
import ops
import pytest

_REAL_REFRESH = core.refresh_installed_hooks   # autouse 夹具会换掉属性，直测要拿原件


@pytest.fixture(autouse=True)
def _keep_real_hooks_untouched(monkeypatch):
    """本文件的用例只验部署校验，不许把真 ~/.agenthub/hooks 当靶子刷一遍。"""
    monkeypatch.setattr(core, "refresh_installed_hooks", lambda *a, **k: ([], []))


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


def test_refresh_installed_hooks_only_touches_mounted_copies(tmp_path):
    """钩子跑的是 ~/.agenthub/hooks 里的副本：已挂载的要刷到最新，没挂的不许新建。

    R833 实测的第三跳漏口：改 .mjs → deploy 报 15/15 一致 → 线上钩子仍是旧文件，
    因为钩子副本只有 hook-install 会写，而 install 要动各家配置，日常发布不会跑。
    偷偷新建等于把用户没启用的钩子激活，所以缺档只能跳过。
    """
    src, dst = tmp_path / "src", tmp_path / "hooks"
    src.mkdir()
    dst.mkdir()
    for name in core.HOOK_SCRIPT_FILES:
        (src / name).write_text("NEW", encoding="utf-8")
    (dst / "brain_inject.mjs").write_text("OLD", encoding="utf-8")   # 已挂载且落后
    (dst / "brain_recall.mjs").write_text("NEW", encoding="utf-8")   # 已挂载且一致
    refreshed, unchanged = _REAL_REFRESH(str(src), str(dst))
    assert refreshed == ["brain_inject.mjs"], (refreshed, unchanged)
    assert "brain_recall.mjs" in unchanged
    assert (dst / "brain_inject.mjs").read_text(encoding="utf-8") == "NEW"
    for name in core.HOOK_SCRIPT_FILES:
        if name in ("brain_inject.mjs", "brain_recall.mjs"):
            continue
        assert not (dst / name).exists(), f"{name} 未挂载，不许被新建"


def test_deploy_prints_hook_refresh(tmp_path, capsys, monkeypatch):
    """deploy 的输出必须说清钩子刷了几个——只报 server 一致会让人以为全线生效。"""
    monkeypatch.setattr(core, "deploy_server", lambda: ("py", str(tmp_path / "srv")))
    monkeypatch.setattr(core, "refresh_installed_hooks",
                        lambda *a, **k: (["brain_error_recall.mjs"], ["brain_inject.mjs"]))
    monkeypatch.setattr(ops, "run_check",
                        lambda *a, **k: {"ok": True, "summary": "15/15 与部署一致", "detail": []})
    rc = cli.main(["deploy", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "钩子副本：刷新 1 个（brain_error_recall.mjs）" in out, out
