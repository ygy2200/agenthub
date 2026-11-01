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


def test_deferred_scan_keeps_selection(qapp, hub_root):
    """后台探测完成重填列表时，不许把用户正选中的那条抢走（R829 撞到的竞态）。

    真机路径：`rescan()` 起 FnWorker 探 agent，回调 `_on_scan` → `fill_memories()` →
    `clear()` 把当前项置空 → 按 R784 收回动作按钮。回调什么时候到达取决于机器负载，
    所以本文件上面两条用例在满负载时会红（HEAD 实测复现，与 R829 的分账改动无关）。
    这里手动走同一条路把时序钉死，不靠 sleep 赌运气。
    """
    import brain
    from helpers import boot_window, settle

    brain.init_db(hub_root)
    mid = brain.add_memory(hub_root, "R829 后台重填时要保住选中", agent="zcode")
    w = boot_window(qapp, hub_root)
    try:
        page = w.hub_page
        w.switchTo(page)
        page.viewCombo.setCurrentIndex(2)
        settle(qapp, 0.8)
        page.memList.setCurrentRow(0)
        settle(qapp, 0.4)
        assert page.delMemBtn.isEnabled(), "用例前提：先要处于已选中态"

        page._on_scan(page.agents)   # 探测回调在「已选中之后」才到达
        settle(qapp, 0.6)
        assert page.delMemBtn.isEnabled(), "后台重填把选中抢走了，删除按钮凭空变灰"
        assert page.cur_path == f"mem:{mid}", f"选中应还在这条记忆上：{page.cur_path!r}"
    finally:
        w.hide()


def test_deferred_scan_drops_selection_when_item_gone(qapp, hub_root):
    """重填时选中条目已不在库里（被删）——必须作废，这是 R784 的底线，不能为了保选中回退。"""
    import brain
    from PySide6.QtCore import Qt
    from helpers import boot_window, settle

    brain.init_db(hub_root)
    gone = brain.add_memory(hub_root, "R829 选中后会被删掉的那条", agent="zcode")
    brain.add_memory(hub_root, "R829 留在列表里的另一条", agent="zcode")
    w = boot_window(qapp, hub_root)
    try:
        page = w.hub_page
        w.switchTo(page)
        page.viewCombo.setCurrentIndex(2)
        settle(qapp, 0.8)
        for i in range(page.memList.count()):
            if page.memList.item(i).data(Qt.UserRole) == f"mem:{gone}":
                page.memList.setCurrentRow(i)
        settle(qapp, 0.4)
        assert page.cur_path == f"mem:{gone}", "用例前提：选中的是要删的那条"

        brain.delete_memory(hub_root, gone, agent="zcode")
        page._on_scan(page.agents)   # 后台重填这一回没有那条了
        settle(qapp, 0.6)
        assert page.cur_path is None, "条目消失后不许继续认旧选中"
        assert not page.delMemBtn.isEnabled(), "删除按钮必须收回，否则下一刀打到别处"
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
