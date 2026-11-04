# -*- coding: utf-8 -*-
"""R846 通道账：MCP 工具通道断了，账本上要看得见。

病史（2026-11-02 实锤，本机）：11-01 19:33 给 `~/.workbuddy/mcp.json` 的 agenthub 条目
加了 `AGENTHUB_SEMANTIC=0`（R832，防 torch 常驻 2GB）。配置一变，客户端记着的授权就不再
匹配，WorkBuddy 从此不再拉起这个 MCP。**接下来连续三轮迭代，我手上一个 `hub_*` 工具都
没有**，全程 `cli.py` 兜底；而账本上 `log_work` / `memory_append` 一行不少，与「MCP 好用」
完全无法区分。用户最后是自己想起来「忘了信任」才修好的。

R845 给流水加了 via，这一项才有判据。用例钉住四件事：
①只有钩子/兜底 → 报断；②有一行 stdio → 不算断；
③探针/内部写入（probe/via/ops/agenthub）不进「某家 agent 通道断」名单；
④改版前的老行（via 为空）**不倒推**，宁可说「未覆盖」也不猜。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brain  # noqa: E402
import ops  # noqa: E402


@pytest.fixture
def hub(hub_root):
    """建表 + 登记两家 agent（通道账只判注册过的名字）。"""
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


def test_只有钩子没有工具时报断(hub):
    _write(hub, "workbuddy", "brain_brief", "hook", 3)
    _write(hub, "workbuddy", "log_work", "cli", 2)
    r = ops._link_health(hub)
    assert r["ok"] is False
    assert "工具通道断" in r["summary"] and "workbuddy" in r["summary"]
    line = [x for x in r["detail"] if x.startswith("workbuddy：")][0]
    assert "MCP 0" in line and "钩子 3" in line and "兜底 2" in line


def test_有一行stdio就不算断(hub):
    _write(hub, "workbuddy", "brain_brief", "hook", 2)
    _write(hub, "workbuddy", "log_work", "stdio")
    r = ops._link_health(hub)
    assert r["ok"] is True
    assert "1 家 MCP 工具在用" in r["summary"]


def test_探针与内部写入不进名单(hub):
    """没登记的名字（probe/via/ops/agenthub）绝不能报成「某家 agent 通道断」——
    第一版就是这么把两条测试流水报成故障的。"""
    _write(hub, "probe", "via_test", "cli")
    _write(hub, "ops", "检查 env", "ops")
    _write(hub, "agenthub", "brain_brief", "cli")
    r = ops._link_health(hub)
    assert r["ok"] is True
    assert "无 agent 可判" in r["summary"]
    assert any("未登记署名" in x and "probe" in x for x in r["detail"])


def test_没via列时明说不判定(hub):
    with brain.db_conn(hub) as conn:
        conn.execute("ALTER TABLE journal DROP COLUMN via")
    _write(hub, "workbuddy", "brain_brief", "hook")
    r = ops._link_health(hub)
    assert r["ok"] is True
    assert "不判定" in r["summary"]


def test_口径起点有声明(hub):
    """via 是今天才有的：老行没口径，报告必须自己说清覆盖起点，不许假装看见了 7 天。"""
    _write(hub, "workbuddy", "log_work", "stdio")
    r = ops._link_health(hub)
    assert any("口径起点" in x for x in r["detail"])


def test_自调不算简报送达(hub):
    """B050：`cli.py call` 每条命令都在进程内握一次手，记一行 brain_brief/initialize，
    agent 名就是服务名 agenthub。不剔除就会在报告里凭空多出一家「收到简报」。"""
    _write(hub, "agenthub", "brain_brief", "cli", 4)
    _write(hub, "workbuddy", "brain_brief", "hook", 2)
    r = ops._brief_hook_health(hub)
    hit = [x for x in r["detail"] if "管理面自调" in x]
    assert hit, r["detail"]
    assert "agenthub 4" in hit[0]
    delivered = [x for x in r["detail"] if x.startswith("近 7 天简报送达")]
    assert delivered and "agenthub" not in delivered[0]


def test_巡检项已注册():
    assert "link_health" in ops.CHECKS
    assert "link_health" in ops.CHECK_FIXES
    assert "link_health" in ops.PATROL_CHECKS


def test_别的数据根的存活进程不作证(hub, monkeypatch):
    """R870：运行标记是全机共享的，别的根的旧进程不能替本库 agent 证明通道还在。

    实锤：部署一改，生产 workbuddy 的旧进程指纹立刻与磁盘失配，把测试假库里的
    「工具通道断」误判成「进程活着、只是代码旧」——检查项必须按 root 过滤。
    """
    import core  # noqa: PLC0415

    monkeypatch.setattr(core, "runtime_markers", lambda *a, **k: [{
        "agent": "workbuddy", "root": str(Path(hub) / "别的根"),
        "base": "C:/fake", "fingerprint": {"brain.py": "stale-digest"},
        "started": "2026-11-04T00:00:00", "version": "2.41.212"}])
    _write(hub, "workbuddy", "brain_brief", "hook", 3)
    r = ops._link_health(hub)
    assert r["ok"] is False, r
    assert "工具通道断" in r["summary"] and "workbuddy" in r["summary"]
