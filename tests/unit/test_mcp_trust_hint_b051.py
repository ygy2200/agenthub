# -*- coding: utf-8 -*-
"""B051：改完 MCP 配置，当场把「客户端要重新信任」说出来。

**同一个坑踩了两次**，两次的过程一字不差：
· 2026-10-10（记录#3118）：mcp.json 的 args 加了 `--agent workbuddy`，路径斜杠也顺带变了
  → 配置指纹变化 → 客户端判定「未信任的新配置」→ 静默不加载。当次就查清了机制，
  结论是「用户去连接器页点一次信任」。
· 2026-11-01：给同一个条目加 `AGENTHUB_SEMANTIC=0`（R832 防 torch 常驻 2GB）→ 同样作废
  → 连跑三轮迭代没有 `hub_*` 工具，全程命令行兜底，账本上看不出异常（`log_work` 一行不少）。

第一次把知识写进了记录，但没有任何东西在「改配置」那一刻说话，所以第二次照旧。
本轮的修法不是再写一遍记录，而是让**写配置的那条路径**自己带警告：
改了内容才提醒（幂等重跑不吵），并给出可自查的判据（`check link_health` 出现 via=stdio）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _adapter(tmp_path, name="x"):
    return {"agent": name, "instr_path": str(tmp_path / "instr.md"),
            "mcp": {"path": str(tmp_path / "mcp.json"), "layout": "standard", "format": "json"}}


def _wire(monkeypatch, cli, tmp_path, real_write=True, name="x"):
    """把 onboard 的写入换成「真写本地临时文件」，其余副作用全部打桩。"""
    a = _adapter(tmp_path, name)
    monkeypatch.setattr(cli.core, "find_adapter", lambda root, agent: a)
    monkeypatch.setattr(cli.core, "_mcp_spec_of",
                        lambda ad: (ad["mcp"]["path"], "json", ["mcpServers"]))
    monkeypatch.setattr(cli.core, "deploy_status",
                        lambda: {"state": "ready", "server": "s", "python": "py",
                                 "diff": [], "absent": [], "same": 0})
    monkeypatch.setattr(cli.core, "deploy_server", lambda: ("py", "s"))
    monkeypatch.setattr(cli.core, "inject_bootstrap", lambda *args, **kw: "")

    def fake_install(path, layout, python_exe, server_py, root, **kw):
        if real_write:                       # 真写文件，才能验「指纹变了才提醒」
            Path(path).write_text('{"mcpServers": {"agenthub": {"command": "py"}}}',
                                  encoding="utf-8")
        return ""

    monkeypatch.setattr(cli.core, "install_mcp_entry", fake_install)
    return a


def test_改了配置就提醒去点信任(tmp_path, monkeypatch, capsys):
    import cli
    _wire(monkeypatch, cli, tmp_path)
    rc = cli.cmd_onboard(str(tmp_path), "x")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "已信任" in out and "信任" in out
    assert "link_health" in out, "提醒必须给可自查的判据，否则还是靠人回忆"


def test_提示写明静默与账本看不出异常(tmp_path, monkeypatch, capsys):
    """光说「要重新信任」不够——用户会以为「不信任顶多报个错」。
    必须说清两件事：静默不加载、以及期间账本照样有 log_work（所以别指望看流水发现）。"""
    import cli
    _wire(monkeypatch, cli, tmp_path)
    cli.cmd_onboard(str(tmp_path), "x")
    out = capsys.readouterr().out
    assert "静默不加载" in out
    assert "log_work" in out


def test_内容没变就不吵(tmp_path, monkeypatch, capsys):
    """幂等重跑（配置一字未改）不该天天弹警告，否则人会把提示当背景噪音关掉。"""
    import cli
    _wire(monkeypatch, cli, tmp_path)
    cli.cmd_onboard(str(tmp_path), "x")
    capsys.readouterr()

    _wire(monkeypatch, cli, tmp_path, real_write=False)   # 这次什么都没写
    cli.cmd_onboard(str(tmp_path), "x")
    out = capsys.readouterr().out
    assert "已信任" not in out and "link_health" not in out, out


def test_写失败不提醒信任(tmp_path, monkeypatch, capsys):
    """写失败时该报错，不该顺带教用户去点信任（会让人以为写成功了）。"""
    import cli
    _wire(monkeypatch, cli, tmp_path, real_write=False)

    def boom(*a, **k):
        return "写入失败：磁盘满"

    monkeypatch.setattr(cli.core, "install_mcp_entry", boom)
    rc = cli.cmd_onboard(str(tmp_path), "x")
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "写入失败" in out and "link_health" not in out


def test_指纹工具本身(tmp_path):
    import core
    f = tmp_path / "a.json"
    assert core.file_digest(str(f)) == ""
    f.write_text("{}", encoding="utf-8")
    d1 = core.file_digest(str(f))
    assert len(d1) == 16
    f.write_text('{"a":1}', encoding="utf-8")
    assert core.file_digest(str(f)) != d1


def test_提示文案带agent名():
    import core
    assert "workbuddy" in core.mcp_trust_hint("workbuddy")
    assert "link_health" in core.mcp_trust_hint("")
