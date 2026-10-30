# -*- coding: utf-8 -*-
"""Agent 中心动作按钮跟随选中态（R784）。

空库截图（output/gui-baseline/shots/hub_page.png）里「删除选中记忆」恒亮；更麻烦的是
`cur_path` 只在点条目时更新——切视图/刷新列表后旧路径还在，编辑按钮仍亮，点下去
改到的是上一视图的资产（删记忆同理）。本轮把「有选中」变成动作按钮的唯一开关。
"""
from helpers import boot_window, pump, settle


def test_delete_memory_follows_selection(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "R784 用来检验删除按钮的记忆", kind="note", agent="zcode")
    w = boot_window(qapp, hub_root)
    try:
        page = w.hub_page
        w.switchTo(page)
        page.viewCombo.setCurrentIndex(2)   # 记忆视图
        settle(qapp, 0.8)
        assert page.memList.count() >= 1
        assert not page.delMemBtn.isEnabled(), "未选中记忆时「删除选中记忆」不该可点"

        page.memList.setCurrentRow(0)
        settle(qapp, 0.4)
        assert page.delMemBtn.isEnabled(), "选中记忆后应可点"
        assert page.confirmBtn.isEnabled(), "选中记忆后「复核选中」应可点"

        # 切到技能视图：上一视图的选中必须作废，否则删除/编辑会打到旧对象
        page.viewCombo.setCurrentIndex(0)
        settle(qapp, 0.6)
        assert page.cur_path is None, "切视图后 cur_path 必须清空"
        assert not page.delMemBtn.isEnabled(), "切视图后删除按钮必须收回"
        assert not page.editBtn.isEnabled(), "切视图后编辑按钮必须收回"
        assert not page.confirmBtn.isEnabled(), "切视图后复核按钮必须收回"
        with brain.db_conn(hub_root) as conn:
            row = conn.execute("SELECT status FROM memories WHERE id=?", (mid,)).fetchone()
        assert row and row[0] != "deleted", "只是切视图，不该动到记忆"
    finally:
        w.hide()


def test_list_clear_resets_actions(qapp, hub_root):
    import brain

    brain.init_db(hub_root)
    brain.add_memory(hub_root, "R784 列表清空后动作要收回", agent="zcode")
    w = boot_window(qapp, hub_root)
    try:
        page = w.hub_page
        w.switchTo(page)
        page.viewCombo.setCurrentIndex(2)
        settle(qapp, 0.8)
        page.memList.setCurrentRow(0)
        settle(qapp, 0.4)
        assert page.delMemBtn.isEnabled()

        page.memFilter.setText("绝对不会匹配的词")
        assert pump(qapp, lambda: page.memList.count() == 1, 5)
        settle(qapp, 0.4)
        assert page.cur_path is None, "过滤后列表重填，旧选中必须作废"
        assert not page.delMemBtn.isEnabled()
    finally:
        w.hide()
