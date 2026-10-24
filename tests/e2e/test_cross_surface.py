# -*- coding: utf-8 -*-
"""e2e 层 R95：同一治理真相，跨面一致（MCP 会话 / CLI 管理面 / brain / 巡检）。

R86/R88/R90/R92/R93/R94 把待审批、交接、拦截铺到 MCP/CLI/桌面/巡检四个出口，
但各面只有自己的单测——没有一条用例证明它们对**同一个库**给出同一个答案。
面越多，口径漂移越隐蔽：管理者在桌面看到 1 条待批、CLI 说 2 条，谁都不敢信。

给定同时存在「30h 待批 / 4 天陈旧交接 / 身份+角色拦截」的库：
  · hub_tasks(view=approvals) 与 cli tasks --approvals 报同一队列、同一老化标记；
  · hub_handoff(list) 与 cli handoffs 报同一交接、同一陈旧标记；
  · hub_status(view="security") 与 cli security 报同一总数与分类；
  · governance_health 的积压数字与三面一致；
  · 只读视图不改治理状态（拦截计数不增）；
  · 跨面状态机：MCP 作废的交接 CLI 立刻可见；CLI 批准的待批任务 MCP 队列立刻消失。
"""
import contextlib
import datetime
import io


def _mcp_tool(root, mid, name, args, agent="zcode"):
    """按真实 stdio 会话路径调用：session=True + 锁定身份（--agent）。"""
    import agenthub_mcp
    old = agenthub_mcp.SERVER_AGENT
    agenthub_mcp.SERVER_AGENT = agent
    try:
        resp = agenthub_mcp.handle_message(
            {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
             "params": {"name": name, "arguments": args}}, root, session=True)
    finally:
        agenthub_mcp.SERVER_AGENT = old
    result = resp.get("result") or {}
    text = (result.get("content") or [{}])[0].get("text", "")
    return text, bool(result.get("isError"))


def _cli(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


def _seed(root):
    """造一个同时触发三类治理信号的大脑；返回各对象 id。"""
    import brain
    brain.ensure_schema(root)
    now = datetime.datetime.now()
    iso = lambda h: (now - datetime.timedelta(hours=h)).isoformat(timespec="seconds")
    e1, aged = brain.task_add(root, "跨面-待批过久", project="跨面",
                              approval_required=True)
    e2, fresh = brain.task_add(root, "跨面-刚认领", project="跨面",
                               approval_required=True)
    assert not e1 and not e2, (e1, e2)
    assert brain.task_claim(root, aged, "zcode")[0] == ""
    assert brain.task_claim(root, fresh, "dsh")[0] == ""
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (iso(30), aged))
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (iso(1), fresh))

    old_msg = brain.create_handoff(root, "跨面", "zcode", done="老交接", next_step="接手")
    new_msg = brain.create_handoff(root, "跨面", "zcode", done="新交接", next_step="观察")
    assert old_msg.startswith("已创建交接 #") and new_msg.startswith("已创建交接 #"), \
        (old_msg, new_msg)
    old_h = int(old_msg.split("#", 1)[1].split("（", 1)[0])
    new_h = int(new_msg.split("#", 1)[1].split("（", 1)[0])
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE handoffs SET created=? WHERE id=?", (iso(4 * 24), old_h))

    brain.journal_add(root, "zcode", "越权拦截", target="hub_claim:identity",
                      note="未锁定身份")
    brain.journal_add(root, "dsh", "越权拦截", target="hub_freeze", note="角色不足")
    return {"aged": aged, "fresh": fresh, "old_h": old_h, "new_h": new_h}


def _deny_count(root):
    import brain
    with brain.db_conn(root) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM journal WHERE action='越权拦截'").fetchone()[0]


def test_governance_surfaces_agree(hub_root):
    """四面对同一库报同一队列、同一老化、同一陈旧、同一拦截分类。"""
    ids = _seed(hub_root)
    import brain
    import cli
    import ops

    deny_before = _deny_count(hub_root)

    # brain 真相：两条待批，只有 30h 的那条老化
    rows = brain.task_approvals(hub_root)
    brain.task_annotate(hub_root, rows)
    assert len(rows) == 2, rows
    assert sum(1 for t in rows if t.get("_approval_aging")) == 1, rows

    text, err = _mcp_tool(hub_root, 10, "hub_tasks", {"view": "approvals"})
    assert not err, text
    assert "待审批任务 2 条" in text, text
    assert text.count("⚠待批过久") == 1, text
    assert f"#{ids['aged']}" in text and "等批 30 小时" in text, text

    rc, out = _cli(cli.cmd_tasks, hub_root, approvals=True)
    assert rc == 0, rc
    assert "待审批任务 2 条" in out, out
    assert out.count("⚠待批过久") == 1, out
    assert f"#{ids['aged']}" in out and "等批 30 小时" in out, out

    # R98：日常状态页与专门队列同数同老化口径（开工对齐不必再查第二处）
    text, err = _mcp_tool(hub_root, 13, "hub_status", {})
    assert not err, text
    assert "待审批 2 条" in text and "待批过久 1" in text, text
    assert text.count("⚠待批过久") == 1, text
    assert "等批 30 小时" in text, text

    # 交接：两条 open，只有 4 天的那条陈旧
    hs = brain.list_handoffs(hub_root)
    assert len(hs) == 2 and sum(1 for h in hs if h.get("_stale")) == 1, hs

    text, err = _mcp_tool(hub_root, 11, "hub_handoff", {"action": "list"})
    assert not err, text
    assert "交接清单（open，新在前）2 条" in text, text
    assert text.count("⚠陈旧") == 1, text

    rc, out = _cli(cli.cmd_handoffs, hub_root)
    assert rc == 0, rc
    assert "交接 2 条（open，新在前）" in out, out
    assert out.count("⚠陈旧") == 1, out

    # 安全：总数与分类三面一致
    sec = brain.security_snapshot(hub_root)
    assert (sec["total"], sec["identity"], sec["role"]) == (2, 1, 1), sec
    agents = {a["agent"]: (a["count"], a["identity"], a["role"])
              for a in sec["by_agent"]}
    assert agents == {"zcode": (1, 1, 0), "dsh": (1, 0, 1)}, sec["by_agent"]

    text, err = _mcp_tool(hub_root, 12, "hub_status", {"view": "security"})
    assert not err, text
    assert "安全拦截（近 7 天）：2 次（未锁身份 1 · 角色越权 1）" in text, text
    assert "按 agent：" in text and "@zcode 1 次" in text and "@dsh 1 次" in text, text

    rc, out = _cli(cli.cmd_security, hub_root)
    assert rc == 0, rc
    assert "安全拦截（近 7 天）：2 次（未锁身份 1 / 角色越权 1）" in out, out
    assert "按 agent：" in out and "@zcode 1 次" in out and "@dsh 1 次" in out, out

    # 巡检：四类信号收口与三面同数
    g = ops.run_check("governance_health", "", hub_root)
    assert not g["ok"], g
    for piece in ("待批过久 1", "陈旧交接 1", "未锁身份拦截 1"):
        assert piece in g["summary"], g["summary"]
    assert _deny_count(hub_root) == deny_before, "只读视图不得写拦截审计"


def test_governance_state_machine_across_surfaces(hub_root):
    """MCP 作废 → CLI 立刻可见；CLI 批准 → MCP 队列立刻消失。"""
    ids = _seed(hub_root)
    import brain
    import cli
    import ops

    text, err = _mcp_tool(hub_root, 20, "hub_handoff", {
        "action": "close", "handoff_id": ids["old_h"],
        "close_note": "跨面验证", "agent": "zcode"})
    assert not err and text.startswith("已关闭交接"), text

    rc, out = _cli(cli.cmd_handoffs, hub_root)
    assert rc == 0 and "交接 1 条（open，新在前）" in out, out
    rc, out_all = _cli(cli.cmd_handoffs, hub_root, include_all=True)
    assert rc == 0 and f"#{ids['old_h']}" in out_all, out_all
    assert "已作废" in out_all and "跨面验证" in out_all, out_all
    g2 = ops.run_check("governance_health", "", hub_root)
    assert "陈旧交接" not in g2["summary"], g2["summary"]

    rc, out = _cli(cli.cmd_approve, hub_root, ids["aged"], "dsh", "跨面验收")
    assert rc == 0 and "已批准完成" in out, out
    left = brain.task_approvals(hub_root)
    assert [t["id"] for t in left] == [ids["fresh"]], left
    text, err = _mcp_tool(hub_root, 21, "hub_tasks", {"view": "approvals"})
    assert not err and "待审批任务 1 条" in text, text
    assert f"#{ids['aged']}" not in text and f"#{ids['fresh']}" in text, text
    g3 = ops.run_check("governance_health", "", hub_root)
    assert "待批过久" not in g3["summary"], g3["summary"]

    # R100 跨面：CLI 退回 fresh（owner=dsh，管理面代退）→ MCP 待批队列立刻清空、
    # 待办看板立刻出现 ↩ 退回留痕；重新认领后 MCP 队列又可见（复核要求跨面一致）。
    rc, out = _cli(cli.cmd_reject, hub_root, ids["fresh"], "跨面退回")
    assert rc == 0 and "已退回返工" in out and "跨面退回" in out, (rc, out)
    assert brain.task_approvals(hub_root) == [], brain.task_approvals(hub_root)
    text, err = _mcp_tool(hub_root, 22, "hub_tasks", {"view": "approvals"})
    assert not err and "无待审批任务" in text, text
    text, err = _mcp_tool(hub_root, 23, "hub_tasks", {"status": "pending"})
    assert not err and "↩退回 1 次（管理面）" in text and "跨面退回" in text, text
    assert f"#{ids['fresh']}" in text, text
    assert brain.task_claim(hub_root, ids["fresh"], "dsh")[0] == ""
    text, err = _mcp_tool(hub_root, 24, "hub_tasks", {"view": "approvals"})
    assert not err and f"#{ids['fresh']}" in text, text
