# -*- coding: utf-8 -*-
"""R848：`link_health` 不许把「进程好好活着、只是代码旧」误报成「通道断」。

为什么这轮必须先修误报：`link_health` 是 R846 刚上线的，它存在的全部意义就是「被信」。
via 口径是 R845 才有的——跑着旧代码的 MCP 进程一个 via 行都写不出来，只看流水就会
把「连接正常」判成「断了」。这种误报一次，人就会把这个检查项当背景噪音永久忽略，
等于没上线。判据不用版本号（版本只是时间的大概），用运行标记里的**进程指纹**：
该进程加载的 `brain.py` md5 与当前部署的那份不一致，就是旧进程。
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import brain  # noqa: E402
import core  # noqa: E402
import ops  # noqa: E402


@pytest.fixture
def hub(hub_root):
    brain.ensure_schema(hub_root)
    with brain.db_conn(hub_root) as conn:
        for name in ("workbuddy", "qoder"):
            conn.execute("INSERT OR REPLACE INTO agents(name,last_seen) VALUES(?,?)",
                         (name, brain._now()))
    return hub_root


def _write(root, agent, action, via, n=1):
    with brain.db_conn(root) as conn:
        for _ in range(n):
            brain._journal_append(conn, brain._now(), agent, action, "", "", "", via)


def _deployed_brain_md5():
    p = Path(core.MCP_SERVER_DIR) / "brain.py"
    if not p.is_file():
        pytest.skip("部署目录里没有 brain.py")
    return hashlib.md5(p.read_bytes()).hexdigest()


def _marker(agent, brain_md5, root=""):
    # R870：运行标记是全机共享的，必须带上数据根；不带 root 的标记不能替本库作证。
    return {"pid": 4242, "agent": agent, "root": str(root),
            "started": brain._now(), "base": str(core.MCP_SERVER_DIR),
            "fingerprint": {"brain.py": brain_md5}}


def test_旧进程不报断而是报待重启(hub, monkeypatch):
    _write(hub, "workbuddy", "brain_brief", "hook", 2)
    monkeypatch.setattr(core, "runtime_markers",
                        lambda *a, **k: [_marker("workbuddy", "0" * 32, root=hub)])
    r = ops._link_health(hub)
    assert r["ok"] is True, r["detail"]
    assert "待重启" in r["summary"]
    line = [x for x in r["detail"] if x.startswith("workbuddy：")][0]
    assert "旧进程" in line and "工具通道断" not in line
    assert any("加载的是旧代码" in x for x in r["detail"])


def test_进程指纹与部署一致时仍然算断(hub, monkeypatch):
    """进程跑的是新码却一条 via 行都没有——这才可疑，照实报断，别拿「旧进程」当挡箭牌。"""
    _write(hub, "workbuddy", "brain_brief", "hook", 2)
    monkeypatch.setattr(core, "runtime_markers",
                        lambda *a, **k: [_marker("workbuddy", _deployed_brain_md5(), root=hub)])
    r = ops._link_health(hub)
    assert r["ok"] is False
    assert "工具通道断" in r["summary"]


def test_没有活进程就按断报(hub, monkeypatch):
    _write(hub, "workbuddy", "brain_brief", "hook", 2)
    monkeypatch.setattr(core, "runtime_markers", lambda *a, **k: [])
    r = ops._link_health(hub)
    assert r["ok"] is False and "工具通道断" in r["summary"]


def test_旧进程不掩盖另一家的真断(hub, monkeypatch):
    """旧进程只豁免它自己：另一家没进程也没 via 行，仍然要点名。"""
    _write(hub, "workbuddy", "brain_brief", "hook", 2)
    _write(hub, "qoder", "log_work", "cli", 3)
    monkeypatch.setattr(core, "runtime_markers",
                        lambda *a, **k: [_marker("workbuddy", "0" * 32, root=hub)])
    r = ops._link_health(hub)
    assert r["ok"] is False
    head = r["summary"].split("——")[-1].split("（另有")[0]
    assert "qoder" in head and "workbuddy" not in head, r["summary"]
    assert "待重启" in r["summary"]


def test_标记读不到不影响判定(hub, monkeypatch):
    """运行标记是旁路观测，坏掉/读不到时不能把整项拖垮——退回「按流水判」。"""
    _write(hub, "workbuddy", "brain_brief", "hook", 2)

    def boom(*a, **k):
        raise OSError("标记目录没了")

    monkeypatch.setattr(core, "runtime_markers", boom)
    r = ops._link_health(hub)
    assert r["ok"] is False and "工具通道断" in r["summary"]
