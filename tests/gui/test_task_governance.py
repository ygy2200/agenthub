# -*- coding: utf-8 -*-
"""gui 层：任务治理可见性（R7，2026-10-21）。

守护：过期租约与逾期任务在桌面任务看板上必须**看得出来**，并且只标真正有问题的
任务——新鲜认领不能被误标（误报会让治理标识失去可信度）。
"""
from PySide6.QtWidgets import QLabel

from helpers import boot_window, pump


def _age_claim(root, tid):
    import brain
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE tasks SET claimed_at='2020-01-01T00:00:00', "
                     "updated='2020-01-01T00:00:00' WHERE id=?", (tid,))


def test_task_board_marks_stale_lease_and_overdue(qapp, hub_root):
    import brain
    import tokens

    brain.init_db(hub_root)
    _, stale_id = brain.task_add(hub_root, "悬空认领任务", project="甲-项目",
                                 due="2020-01-01")
    brain.task_claim(hub_root, stale_id, "zcode")
    _age_claim(hub_root, stale_id)
    _, fresh_id = brain.task_add(hub_root, "新鲜任务", project="甲-项目")
    brain.task_claim(hub_root, fresh_id, "dsh")

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.brain_page
    w.switchTo(page)
    page.reload()

    assert pump(qapp, lambda: any("悬空认领任务" in lb.text()
                                  for lb in page.findChildren(QLabel)), 10), "任务看板未加载"
    labels = page.findChildren(QLabel)
    texts = [lb.text() for lb in labels]
    assert any(t.startswith("逾期 ") for t in texts), texts
    stale_chips = [lb for lb in labels if lb.text() == "租约过期"]
    assert len(stale_chips) == 1, f"新鲜任务被误标或过期任务漏标：{texts}"
    assert "hub_claim" in stale_chips[0].toolTip(), stale_chips[0].toolTip()
    assert any("新鲜任务" in t for t in texts), texts


def test_task_board_shows_file_lease_chip(qapp, hub_root):
    """R14：挂在本任务上的文件租约在桌面可见（与 hub_tasks 的「锁 N」同口径），
    未挂锁的任务不得被误标；tooltip 给出路径/持有者/剩余时间。"""
    import brain
    import tokens

    brain.init_db(hub_root)
    _, tid = brain.task_add(hub_root, "改解析器", project="甲-项目")
    assert brain.task_claim(hub_root, tid, "zcode")[0] == ""
    err, _ = brain.file_lease_acquire(hub_root, "甲-项目/parser.py", "zcode",
                                      task_id=tid, ttl_min=30, reason="重构")
    assert err == "", err
    _, other = brain.task_add(hub_root, "不碰文件", project="甲-项目")
    assert brain.task_claim(hub_root, other, "dsh")[0] == ""

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.brain_page
    w.switchTo(page)
    page.reload()

    assert pump(qapp, lambda: any("改解析器" in lb.text()
                                  for lb in page.findChildren(QLabel)), 10), "任务看板未加载"
    labels = page.findChildren(QLabel)
    chips = [lb for lb in labels if lb.text() == "文件锁 1"]
    assert len(chips) == 1, [lb.text() for lb in labels]
    tip = chips[0].toolTip()
    assert "甲-项目/parser.py" in tip and "zcode" in tip and "剩" in tip, tip


def test_task_board_shows_approval_age(qapp, hub_root):
    """R94：待批任务在桌面显示等批时长；≥24h 标「待批过久」并给出审批入口，
    未超阈值的也显示时长但不得误标过久（与 hub_tasks --approvals 同口径）。"""
    import datetime

    import brain
    import tokens

    brain.init_db(hub_root)
    _, aged = brain.task_add(hub_root, "等批过久任务", project="甲-项目",
                             approval_required=True)
    _, fresh = brain.task_add(hub_root, "刚进队列任务", project="甲-项目",
                              approval_required=True)
    assert brain.task_claim(hub_root, aged, "zcode")[0] == ""
    assert brain.task_claim(hub_root, fresh, "dsh")[0] == ""
    stale_ts = (datetime.datetime.now() - datetime.timedelta(hours=30)) \
        .isoformat(timespec="seconds")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (stale_ts, aged))

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.brain_page
    w.switchTo(page)
    page.reload()

    assert pump(qapp, lambda: any("等批过久任务" in lb.text()
                                  for lb in page.findChildren(QLabel)), 10), "任务看板未加载"
    labels = page.findChildren(QLabel)
    chips = [lb for lb in labels if lb.text().startswith("待批 ")]
    assert len(chips) == 2, [lb.text() for lb in labels]
    old = [lb for lb in chips if lb.text() == "待批 30h"]
    assert len(old) == 1, [lb.text() for lb in chips]
    assert "待批过久" in old[0].toolTip() and "hub_task_approve" in old[0].toolTip(), \
        old[0].toolTip()
    young = [lb for lb in chips if lb.text() != "待批 30h"][0]
    assert "待批过久" not in young.toolTip(), young.toolTip()
    assert "hub_task_approve" in young.toolTip(), young.toolTip()


def test_security_page_shows_governance_health(qapp, hub_root):
    """R94/R99：安全与闸门页直接展示治理巡检结论与积压明细、越权拦截按 agent 聚合；
    有积压时页面摘要必须转为警示态。"""
    import brain
    import tokens

    brain.init_db(hub_root)
    _, tid = brain.task_add(hub_root, "待批积压任务", project="甲-项目",
                            approval_required=True)
    assert brain.task_claim(hub_root, tid, "zcode")[0] == ""
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE tasks SET claimed_at='2020-01-01T00:00:00' WHERE id=?", (tid,))
    brain.journal_add(hub_root, "worker-a", "越权拦截", target="hub_freeze",
                      note="角色不足")
    brain.journal_add(hub_root, "worker-a", "越权拦截", target="hub_claim:identity",
                      note="未锁定身份")

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.governance_page
    w.switchTo(page)
    page.seg.setCurrentItem("security")
    page.reload()

    sub = page.stack.currentWidget()
    assert pump(qapp, lambda: any(
        "治理巡检：" in sub.listw.item(i).text() for i in range(sub.listw.count())), 10), \
        "治理巡检未渲染"
    texts = [sub.listw.item(i).text() for i in range(sub.listw.count())]
    assert any(t.startswith("× 治理巡检：") for t in texts), texts
    assert any("待批积压任务" in t and "hub_task_approve" in t for t in texts), texts
    assert any(t.startswith("越权拦截（近 7 天）：2 次") and "未锁身份 1" in t
               and "角色越权 1" in t for t in texts), texts
    assert any("按 agent：@worker-a 2 次" in t for t in texts), texts
    assert "！安全面有异常" in sub.sumLabel.text(), sub.sumLabel.text()


def test_governance_approvals_page_approves(qapp, hub_root, monkeypatch):
    """R96：治理中心「待审批」是桌面操作面——显示等批时长与过久标记；
    批准走 four-eyes，署名 gui、复核意见进审计链，队列立即移除。"""
    import datetime

    import brain
    import tokens
    import pages.governance_page as gp

    brain.init_db(hub_root)
    _, aged = brain.task_add(hub_root, "等批过久任务", project="甲-项目",
                             approval_required=True)
    _, fresh = brain.task_add(hub_root, "刚进队列任务", project="甲-项目",
                              approval_required=True)
    assert brain.task_claim(hub_root, aged, "zcode")[0] == ""
    assert brain.task_claim(hub_root, fresh, "dsh")[0] == ""
    stale_ts = (datetime.datetime.now() - datetime.timedelta(hours=30)) \
        .isoformat(timespec="seconds")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (stale_ts, aged))

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.governance_page
    w.switchTo(page)
    page.reload()
    sub = page.stack.currentWidget()

    assert pump(qapp, lambda: sub.listw.count() == 2, 10), "待审批列表未加载"
    texts = [sub.listw.item(i).text() for i in range(sub.listw.count())]
    old = [t for t in texts if "等批过久任务" in t]
    new = [t for t in texts if "刚进队列任务" in t]
    assert len(old) == 1 and len(new) == 1, texts
    assert "等批 30h" in old[0] and "！待批过久" in old[0], old[0]
    assert "！待批过久" not in new[0], new[0]

    class _Dialog:
        @staticmethod
        def getText(*_args, **_kwargs):
            return "验收通过", True

    monkeypatch.setattr(gp, "QInputDialog", _Dialog)
    sub.listw.setCurrentRow(0)
    sub.approveBtn.click()
    assert pump(qapp, lambda: [t["id"] for t in brain.task_approvals(hub_root)]
                == [fresh], 10), "批准后队列未移除该任务"
    row = brain.task_get(hub_root, aged)
    assert row.get("approved_by") == "gui", row
    with brain.db_conn(hub_root) as conn:
        note = conn.execute("SELECT note FROM journal WHERE action='任务审批' "
                            "ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "验收通过" in note, note
    assert sub.listw.count() == 1, [sub.listw.item(i).text()
                                     for i in range(sub.listw.count())]

def test_governance_handoffs_page_claims_and_closes(qapp, hub_root, monkeypatch):
    """R97：治理中心「待认领交接」是桌面操作面——显示老化与陈旧标记；
    认领以 gui 署名，作废必须填原因；已认领交接不可再作废，勾选后可见历史状态。"""
    import datetime

    import brain
    import tokens
    import pages.governance_page as gp

    brain.init_db(hub_root)
    brain.create_handoff(hub_root, "甲-项目", "zcode", done="写完解析器",
                         next_step="补边界测试", excluded="不走正则")
    old_h = brain.list_handoffs(hub_root, include_claimed=True)[0]["id"]
    brain.create_handoff(hub_root, "甲-项目", "dsh", done="搭好骨架",
                         next_step="接上数据库")
    fresh_h = brain.list_handoffs(hub_root, include_claimed=True)[0]["id"]
    stale_ts = (datetime.datetime.now() - datetime.timedelta(days=4))         .isoformat(timespec="seconds")
    with brain.db_conn(hub_root) as conn:
        conn.execute("UPDATE handoffs SET created=? WHERE id=?", (stale_ts, old_h))

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.governance_page
    w.switchTo(page)
    page.seg.setCurrentItem("handoffs")
    page.reload()
    sub = page.stack.currentWidget()

    assert pump(qapp, lambda: sub.listw.count() == 2, 10), "交接列表未加载"
    texts = [sub.listw.item(i).text() for i in range(sub.listw.count())]
    old = [t for t in texts if f"#{old_h} " in t]
    fresh = [t for t in texts if f"#{fresh_h} " in t]
    assert old and fresh, texts
    assert "！陈旧" in old[0] and "！陈旧" not in fresh[0], texts
    assert "陈旧 1" in sub.sumLabel.text(), sub.sumLabel.text()

    sub.listw.setCurrentRow(0)
    assert sub._selected()["id"] == fresh_h
    sub.claimBtn.click()
    assert pump(qapp, lambda: brain.list_handoffs(hub_root, include_claimed=True)[0]
                is not None, 5)
    rows = {r["id"]: r for r in brain.list_handoffs(hub_root, include_claimed=True)}
    assert rows[fresh_h]["status"] == "claimed", rows[fresh_h]
    assert rows[fresh_h]["claimed_by"] == "gui", rows[fresh_h]

    sub.allChk.setChecked(True)
    assert pump(qapp, lambda: sub.listw.count() == 2, 10), "含历史开关未生效"
    claimed_texts = [sub.listw.item(i).text() for i in range(sub.listw.count())]
    assert any(f"#{fresh_h} " in t and "已认领" in t for t in claimed_texts), claimed_texts

    claimed_row = next(i for i in range(sub.listw.count())
                       if f"#{fresh_h} " in sub.listw.item(i).text())
    sub.listw.setCurrentRow(claimed_row)
    sub.closeBtn.click()
    rows = {r["id"]: r for r in brain.list_handoffs(hub_root, include_claimed=True)}
    assert rows[fresh_h]["status"] == "claimed", "已认领交接被桌面误作废"

    class _Dialog:
        @staticmethod
        def getText(*_args, **_kwargs):
            return "需求已撤回", True

    monkeypatch.setattr(gp, "QInputDialog", _Dialog)
    old_row = next(i for i in range(sub.listw.count())
                   if f"#{old_h} " in sub.listw.item(i).text())
    sub.listw.setCurrentRow(old_row)
    sub.closeBtn.click()
    assert pump(qapp, lambda: {r["id"]: r for r in brain.list_handoffs(
        hub_root, include_claimed=True)}[old_h]["status"] == "closed", 10), "作废未生效"
    rows = {r["id"]: r for r in brain.list_handoffs(hub_root, include_claimed=True)}
    assert rows[old_h]["closed_by"] == "gui", rows[old_h]
    assert rows[old_h]["close_note"] == "需求已撤回", rows[old_h]
    with brain.db_conn(hub_root) as conn:
        journal = [tuple(r) for r in conn.execute(
            "SELECT agent, action, note FROM journal "
            "WHERE action IN ('认领交接','关闭交接') ORDER BY id")]
    assert any(a == "gui" and ac == "认领交接" for a, ac, _ in journal), journal
    assert any(a == "gui" and ac == "关闭交接" and "需求已撤回" in (n or "")
               for a, ac, n in journal), journal


def test_governance_approvals_page_rejects(qapp, hub_root, monkeypatch):
    """R100：治理中心「待审批」的「退回返工」——空原因前置拦截不落库；
    填原因后退回署名 gui、任务回待办并释放持有权、队列即时移除、审计留「任务退回」。"""
    import brain
    import tokens
    import pages.governance_page as gp

    brain.init_db(hub_root)
    _, tid = brain.task_add(hub_root, "退回返工任务", project="甲-项目",
                            approval_required=True)
    assert brain.task_claim(hub_root, tid, "zcode")[0] == ""

    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.governance_page
    w.switchTo(page)
    page.reload()
    sub = page.stack.currentWidget()
    assert pump(qapp, lambda: sub.listw.count() == 1, 10), "待审批列表未加载"

    # 空原因：前置拦截，不调 brain，任务留在队列
    class _Empty:
        @staticmethod
        def getText(*_args, **_kwargs):
            return "", True

    monkeypatch.setattr(gp, "QInputDialog", _Empty)
    sub.listw.setCurrentRow(0)
    sub.rejectBtn.click()
    assert brain.task_get(hub_root, tid)["status"] == "in_progress", "空原因不该生效"
    assert sub.listw.count() == 1, "空原因后队列不该变化"

    # 填原因退回：任务回待办、署名 gui、队列移除、审计留痕
    class _Reason:
        @staticmethod
        def getText(*_args, **_kwargs):
            return "验收证据不足，请补实测数据", True

    monkeypatch.setattr(gp, "QInputDialog", _Reason)
    sub.rejectBtn.click()
    assert pump(qapp, lambda: sub.listw.count() == 0, 10), "退回后队列未移除"
    row = brain.task_get(hub_root, tid)
    assert row["status"] == "pending" and not (row["owner"] or "").strip(), row
    assert row.get("rejected_by") == "gui" and row.get("reject_count") == 1, row
    assert "验收证据不足" in (row.get("reject_note") or ""), row
    with brain.db_conn(hub_root) as conn:
        note = conn.execute("SELECT note FROM journal WHERE action='任务退回' "
                            "ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "验收证据不足" in note, note
