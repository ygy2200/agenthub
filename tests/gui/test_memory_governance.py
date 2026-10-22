# -*- coding: utf-8 -*-
"""gui 层：记忆治理可见性与复核动作（第五轮 R5）。

守护两件事：
  ① 陈旧/过期记忆在列表里必须**看得出来**（徽标），不能与新鲜记忆同脸；
  ② 「复核选中」必须真的写 confirmed 并让新鲜度读时重置（GUI 不是只读摆设）。
"""
import datetime

from helpers import boot_window, pump


def _seed_stale(root):
    import brain
    brain.init_db(root)
    mid = brain.add_memory(root, "GUI 复核用例：旧事实需要重新确认", kind="fact", agent="zcode")
    old = (datetime.datetime.now() - datetime.timedelta(days=400)).isoformat(timespec="seconds")
    with brain.db_conn(root) as conn:
        conn.execute("UPDATE memories SET created=?, updated=?, last_hit='' WHERE id=?",
                     (old, old, mid))
    return mid


def _row_for(page, mid):
    for i in range(page.memList.count()):
        if f"#{mid} " in page.memList.item(i).text():
            return i
    return -1


def test_memory_view_marks_stale_and_confirms(qapp, hub_root):
    import brain
    import tokens

    mid = _seed_stale(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    page = w.hub_page
    w.switchTo(page)
    page.viewCombo.setCurrentIndex(2)

    assert pump(qapp, lambda: _row_for(page, mid) >= 0, 10), "记忆视图未加载出种子记忆"
    idx = _row_for(page, mid)
    assert "过期" in page.memList.item(idx).text(), \
        f"400 天的 fact 未标过期：{page.memList.item(idx).text()!r}"

    item = page.memList.item(idx)
    page.memList.setCurrentItem(item)
    page.on_preview(item)
    assert page.confirmBtn.isEnabled(), "选中记忆后「复核选中」应可用"
    page.confirm_selected_memory()

    rows = brain.search_memories(hub_root, "GUI 复核用例", "", 5)
    row = next(r for r in rows if r["id"] == mid)
    assert (row.get("confirmed") or "").strip(), "复核后 confirmed 未写入"
    assert brain.memory_freshness(row)["state"] == "fresh", "复核后新鲜度未重置"

    # 落库后列表应即时刷新（不再标过期）
    assert pump(qapp, lambda: "过期" not in page.memList.item(idx).text(), 5), \
        "复核后列表未刷新"
