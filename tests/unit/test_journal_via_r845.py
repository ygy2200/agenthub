# -*- coding: utf-8 -*-
"""R845 传输口径：流水要记「从哪条路进来的」，否则 MCP 掉线三周没人知道。

病史（本机真实体验）：workbuddy 的 MCP 从 11-01 19:33 改过 mcp.json 之后就一直没被
客户端拉起，整轮迭代全靠 `cli.py` 兜底。账本上一片正常——`log_work` / `memory_append`
照样有行，因为兜底和 MCP 写的是同一批 action 名。**只有传输方式这一列能把它们分开**，
所以这一列不是锦上添花，是「通道断了」唯一可验证的判据。

附带解掉 B050：`cli.py call` 每次进程内握手都会记一行 brain_brief/initialize，被
brief_hook_health 当成「这家 agent 收到简报」——有了 via，自调（cli）与真送达（hook/stdio）
一眼可辨。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brain  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_via(monkeypatch):
    """via 是进程级全局态，用例之间必须清干净，否则顺序一变就互相污染。"""
    monkeypatch.setattr(brain, "_VIA", "")
    yield


@pytest.fixture
def hub_root(hub_root):
    """共享夹具建好临时根后补建表——journal 不建表，写入会被 fail-open 静默吞掉。"""
    brain.ensure_schema(hub_root)
    return hub_root


def _rows(root):
    """旧库（无 via 列）也要能读——降级用例本身就要读这种库，不能先崩在 SELECT 上。"""
    with brain.db_conn(root) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(journal)")]
        sel = "id,agent,action,target,note" + (",via" if "via" in cols else "")
        return [dict(r) for r in conn.execute(f"SELECT {sel} FROM journal ORDER BY id")]


def test_默认不设口径时留空(hub_root):
    """没设过就是空串，绝不猜一个默认值——猜成 cli 会把真 MCP 使用判成兜底。"""
    brain.journal_add(hub_root, "t", "act")
    assert [r["via"] for r in _rows(hub_root)] == [""]


def test_进程口径写进流水(hub_root):
    brain.set_via("stdio")
    brain.journal_add(hub_root, "workbuddy", "log_work", "P1")
    assert _rows(hub_root)[-1]["via"] == "stdio"


def test_显式口径压过进程口径(hub_root):
    """巡检跑在 MCP 进程里，但它是后台自己触发的，不能冒充满送达。"""
    brain.set_via("stdio")
    brain.journal_add(hub_root, "ops", "检查 env", "P1", via="ops")
    assert _rows(hub_root)[-1]["via"] == "ops"


def test_口径大小写与空白归一(hub_root):
    brain.set_via("  StdIO ")
    assert brain.get_via() == "stdio"
    brain.set_via(None)
    assert brain.get_via() == ""


def test_口径不进审计链(hub_root):
    """via 是诊断信息，不是审计事实：同一批内容换 via 不能让 journal_verify 报断链。"""
    brain.set_via("cli")
    brain.journal_add(hub_root, "a", "x1", "P1")
    brain.journal_add(hub_root, "a", "x2", "P1", via="hook")
    brain.journal_add(hub_root, "a", "x3", "P1", via="stdio")
    brain.set_via("ops")
    brain.journal_add(hub_root, "a", "x4", "P1")
    res = brain.journal_verify(hub_root)
    assert res["ok"] is True, res
    assert res["broken"] == []
    assert [r["via"] for r in _rows(hub_root)] == ["cli", "hook", "stdio", "ops"]


def test_旧库没via列也能写(hub_root):
    """迁移未跑到时不能丢流水：退回六列写法，行照样落地。"""
    brain.ensure_schema(hub_root)
    with brain.db_conn(hub_root) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(journal)")]
    if "via" in cols:                      # 新库先降级成旧形状
        with brain.db_conn(hub_root) as conn:
            conn.execute("ALTER TABLE journal DROP COLUMN via")
    brain.set_via("stdio")
    brain.journal_add(hub_root, "a", "老库写入", "P1")
    rows = _rows(hub_root)
    assert len(rows) == 1 and rows[0]["action"] == "老库写入"
    assert "via" not in rows[0]


def test_迁移补列且幂等(hub_root):
    brain.ensure_schema(hub_root)
    with brain.db_conn(hub_root) as conn:
        assert "via" in [r[1] for r in conn.execute("PRAGMA table_info(journal)")]
    brain.ensure_schema(hub_root)           # 第二次不报错
    brain.set_via("hook")
    brain.journal_add(hub_root, "a", "迁移后写入", "P1")
    assert _rows(hub_root)[-1]["via"] == "hook"


def test_三通道互不冒充():
    """入口写法钉死：stdio/cli/hook/ops 四个来源必须在各自入口 main/serve 里设。"""
    root = Path(__file__).resolve().parents[2]   # tests/unit/x.py -> 仓库根
    expect = {
        "agenthub_mcp.py": 'brain.set_via("stdio")',
        "cli.py": '_brain.set_via("cli")',
        "brain_brief.py": 'brain.set_via("hook")',
        "brain_recall.py": 'brain.set_via("hook")',
        "ops.py": 'via="ops"',
    }
    for fn, needle in expect.items():
        src = (root / fn).read_text(encoding="utf-8")
        assert needle in src, f"{fn} 少了传输口径标记：{needle}"
