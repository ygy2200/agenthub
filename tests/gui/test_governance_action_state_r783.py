# -*- coding: utf-8 -*-
"""治理中心动作按钮跟随选中态（R783）。

R198 只修了工作台与编排中心。空库走查（output/gui-baseline/shots）里治理中心
「批准完成 / 退回返工」在队列为空、无选中时仍是亮的：点下去只弹一句「先在列表里
选中一条」——按钮亮着却不能用，比灰着更费解。
"""
from helpers import boot_window, pump, settle


def _seed_approval(root, title="等批任务"):
    import brain
    _, tid = brain.task_add(root, title=title, project="R783", approval_required=True)
    err = brain.task_claim(root, tid, "zcode")[0]
    assert err == "", err
    return tid


def _seed_handoff(root):
    import brain
    msg = brain.create_handoff(root, project="R783", agent="zcode",
                               done="做到一半", next_step="接着做", excluded="别的路")
    assert msg.startswith("已创建交接"), msg


def test_approval_buttons_follow_selection(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        page = w.governance_page
        w.switchTo(page)
        page.reload()
        sub = page.stack.currentWidget()
        settle(qapp, 1.0)
        assert sub.listw.count() == 0
        assert not sub.approveBtn.isEnabled(), "空队列时「批准完成」不该可点"
        assert not sub.rejectBtn.isEnabled(), "空队列时「退回返工」不该可点"

        _seed_approval(hub_root)
        sub.reload()
        assert pump(qapp, lambda: sub.listw.count() == 1, 10), "待审批未加载"
        assert not sub.approveBtn.isEnabled(), "有条目但未选中时不该可点"

        sub.listw.setCurrentRow(0)
        settle(qapp, 0.4)
        assert sub.approveBtn.isEnabled(), "选中后「批准完成」应可点"
        assert sub.rejectBtn.isEnabled(), "选中后「退回返工」应可点"

        sub.listw.setCurrentRow(-1)
        settle(qapp, 0.4)
        assert not sub.approveBtn.isEnabled(), "取消选中后应收回可点态"
    finally:
        w.hide()


def test_handoff_buttons_follow_selection(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        page = w.governance_page
        w.switchTo(page)
        page.reload()
        # 切到「待认领交接」段
        page.seg.setCurrentItem("handoffs")
        settle(qapp, 0.6)
        sub = page.stack.currentWidget()
        settle(qapp, 0.8)
        for btn in (sub.claimBtn, sub.closeBtn):
            assert not btn.isEnabled(), "空交接队列时动作按钮不该可点"

        _seed_handoff(hub_root)
        sub.reload()
        assert pump(qapp, lambda: sub.listw.count() >= 1, 10), "交接未加载"
        assert not sub.claimBtn.isEnabled(), "有条目但未选中时不该可点"
        sub.listw.setCurrentRow(0)
        settle(qapp, 0.4)
        assert sub.claimBtn.isEnabled(), "选中交接后「认领」应可点"
        assert sub.closeBtn.isEnabled(), "选中交接后「作废」应可点"
    finally:
        w.hide()
