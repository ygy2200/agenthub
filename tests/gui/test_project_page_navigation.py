# -*- coding: utf-8 -*-
"""R168 项目页长记录导航：逐条渲染不截断 + 下拉跳锚点 + 空项目降级。

背景：真实 AgentHub 项目 359 条记录 / 34.6 万字符。旧实现把全部记录拼成一段
再 md_to_html（默认 100KB 上限），后 2/3 记录静默消失且没有任何跳转入口。
"""
from PySide6.QtCore import Qt

from helpers import boot_window, insert_records, pump, settle

LONG_TITLE = "最后一条-含特殊字符 <b>R168</b>"
BIG = "段落内容R168。" * 180


def _seed(root):
    import brain
    brain.init_db(root)
    insert_records(root, 120, projects=1)
    with brain.db_conn(root) as conn:
        for i in range(120):
            conn.execute("UPDATE records SET content=? WHERE title=?",
                         (BIG + f" tail{i}", f"r{i}"))
        conn.execute("UPDATE records SET title=? WHERE title='r0'", (LONG_TITLE,))
        conn.execute("INSERT OR IGNORE INTO projects(name,created) VALUES(?,?)",
                     ("Z-空项目", "2026-10-11"))


def test_project_records_render_fully_and_jump_by_anchor(qapp, hub_root):
    _seed(hub_root)
    w = boot_window(qapp, hub_root)
    try:
        pp = w.project_page
        assert pump(qapp, lambda: pp.snap is not None, 30)
        w.switchTo(pp)
        settle(qapp, 1.0)
        pp.filterEdit.setText("P0")
        settle(qapp, 2.0)
        assert pp.current == "P0"
        assert pp.recCombo.count() == 120
        assert pp.recCombo.isEnabled()
        assert "120" in pp.recCount.text()
        assert pp.browser.verticalScrollBar().maximum() > 0

        # 100KB 截断回归：120×~1.3KB 正文，全文必须进浏览器
        text = pp.browser.toPlainText()
        assert len(text) > 150_000, f"记录正文疑似被截断：{len(text)} 字"
        assert "结尾标记" not in text  # 上面只是占位正文，确认不是旧种子
        assert "tail119" in text
        assert "tail0" in text
        # 标题按纯文本转义，<b> 不被当作标签
        assert "<b>R168</b>" in text
        assert LONG_TITLE in pp.recCombo.itemData(119)["full"]

        # 跳到最后一条（最旧）：滚动位置必须显著下移，且下拉回到占位态
        sb = pp.browser.verticalScrollBar()
        pp.jump_record(119)
        settle(qapp, 1.0)
        last_pos = sb.value()
        assert last_pos > 0
        assert last_pos >= sb.maximum() * 0.7, f"未跳到末尾：{last_pos}/{sb.maximum()}"
        assert pp.recCombo.currentIndex() == -1

        # 跳到中段：位置介于顶部与末尾之间
        pp.jump_record(60)
        settle(qapp, 1.0)
        mid_pos = sb.value()
        assert 0 < mid_pos < last_pos, (mid_pos, last_pos)

        # 切换项目再切回：回到顶部，下拉无残留
        pp.filterEdit.setText("Z-空项目")
        settle(qapp, 1.0)
        assert pp.current == "Z-空项目"
        assert not pp.recCombo.isEnabled()
        assert pp.recCombo.count() == 0
        assert "共 0 条" in pp.recCount.text()
        assert pp.browser.verticalScrollBar().value() == 0
        pp.filterEdit.setText("P0")
        settle(qapp, 1.0)
        assert pp.recCombo.currentIndex() == -1
        assert pp.browser.verticalScrollBar().value() == 0
    finally:
        # closeEvent 会排 2s 硬退出定时器，同进程多用例必须 hide
        w.hide()
