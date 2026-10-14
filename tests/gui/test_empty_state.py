# -*- coding: utf-8 -*-
"""gui 层：空列表必须有空态引导（UI 缺陷自查 Round 5 / 2026-10-13）。

v2.25 立了「空态组件化」标准（三类文案：首次空=教 / 清空空=确认 / 筛选空=恢复路径），
但**只在源码里、没有守卫**——后续新增/合并的页面全漏了。实测空库完整刷新后，
**10 个列表是空的却没有任何引导**：治理中心 4 个、项目 2 个、工作台 1 个、
流水·对账 1 个、运营报告 1 个、搜索 1 个（用户首次进入就是一片空白）。

修法：uikit.list_empty_guard(listw, layout, title, desc) —— 懒创建 + 按 count 显隐，
页面在 reload() 末尾调一次。已铺到上述全部页面。

本测试用**空数据实测**（不是读代码）：给 uikit.empty_state 打标签后统计引导出现次数。
注意只 patch uikit.empty_state —— list_empty_guard 在 uikit 内部按全局名调用它，
所以能拦住；页面自身直接调用的空态不受影响（那些页面本来就有）。
"""
from PySide6.QtWidgets import QListWidget, QWidget

from helpers import boot_window, pump


def test_empty_lists_have_guidance(qapp, hub_root):
    """空库下，每个为空的列表都必须有空态引导。"""
    import actions
    import brain
    import tokens
    import uikit

    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    orig = uikit.empty_state

    def tagged(*a, **k):
        w = orig(*a, **k)
        w.setObjectName("EMPTY_STATE_TEST")
        return w

    uikit.empty_state = tagged
    try:
        w = boot_window(qapp, hub_root)
        assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 40), \
            "扫描未完成"
        bad = []
        for attr, title, *_rest in actions.PAGES:
            page = getattr(w, attr, None)
            if page is None:
                continue
            w.switchTo(page)
            for _ in range(8):
                qapp.processEvents()
            hints = len(page.findChildren(QWidget, "EMPTY_STATE_TEST"))
            empty = [lw for lw in page.findChildren(QListWidget) if lw.count() == 0]
            if empty and not hints:
                bad.append(f"{title}（{len(empty)} 个空列表）")
        # 空态挂在 reload/填充路径里，所以也要真跑一次填充动作：
        # Round 5 时 run() 里引用了 __init__ 的局部变量 lay，只有这里跑一次才会炸 NameError
        # （空数据快照路径不经过 run()，故之前漏掉）。
        w.search_page.edit.setText("测试")
        w.search_page.run()
        for _ in range(8):
            qapp.processEvents()
        w.search_page.edit.setText("")
        w.search_page.run()
        for _ in range(8):
            qapp.processEvents()
        assert not bad, f"空列表无空态引导（用户看到一片空白）：{bad}"
    finally:
        uikit.empty_state = orig
