# -*- coding: utf-8 -*-
"""gui 层：每个顶层页都要有「标题 + 定位语」页头（UI 缺陷自查 Round 4 / 2026-10-13）。

v2.25 立的标准：每页一句话定位（这页回答什么问题）。但标准只在源码里、没有守卫，
之后新增/合并页面就漏了——实测全库唯一没页头的顶层页是「流水·对账」
（LedgerPage 合并原流水+对账两页时，直接进分段控件，没带头）。
子页（audit_page / journal_page）由容器承载、只有分节标题，不计入。
"""
from helpers import boot_window, pump


def test_all_top_pages_have_title(qapp, hub_root):
    """顶层页必须渲染出页头标题（TitleLabel）。"""
    from PySide6.QtWidgets import QLabel

    import actions
    import brain
    import tokens
    brain.init_db(hub_root)
    tokens.apply_theme("dark")
    w = boot_window(qapp, hub_root)
    assert pump(qapp, lambda: getattr(w.project_page, "snap", None) is not None, 30), \
        "扫描未完成"

    missing = []
    for attr, title, *_rest in actions.PAGES:
        page = getattr(w, attr, None)
        if page is None:
            continue
        w.switchTo(page)
        for _ in range(6):
            qapp.processEvents()
        has_title = any(type(x).__name__ == "TitleLabel" for x in page.findChildren(QLabel))
        if not has_title:
            missing.append(title)
    assert not missing, f"顶层页缺页头标题（v2.25 每页定位标准）：{missing}"
