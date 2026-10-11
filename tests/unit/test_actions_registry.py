# -*- coding: utf-8 -*-
"""unit 层：action 注册表完整性（P2-2 同源表的守门测试）。"""


def test_pages_registry_integrity():
    import actions
    attrs = [p[0] for p in actions.PAGES]
    assert len(attrs) == len(set(attrs)), "页面 attr 重复"
    titles = [p[1] for p in actions.PAGES]
    assert len(titles) == len(set(titles)), "页面标题重复"
    # Ctrl 序号：1..9 连续且不重复（与既有快捷键体系一致）
    ctrls = sorted(p[5] for p in actions.PAGES if p[5])
    assert ctrls == list(range(1, 10)), f"Ctrl 序号应恰为 1..9，实际 {ctrls}"
    # 分组：必须落在已知分组集合内
    known_groups = {"top", "bottom"} | {g for g, _ in actions.GROUP_HEADERS}
    for p in actions.PAGES:
        assert p[3] in known_groups, f"{p[0]} 分组未知：{p[3]}"
        assert p[2] and p[4], f"{p[0]} 缺图标或关键词"


def test_actions_registry_integrity():
    import actions
    ids = [a[0] for a in actions.ACTIONS]
    assert len(ids) == len(set(ids)), "动作 id 重复"
    for a in actions.ACTIONS:
        assert len(a) == 4 and a[1] and a[2] and a[3], f"动作条目不完整：{a}"
    # 三个主题动作必须都在（设置页/命令面板依赖）
    assert {"theme_dark", "theme_light", "theme_auto"} <= set(ids)


def test_pages_cover_bottom_group_and_components():
    """组件预览页（P2-3c）必须已注册，且底部组包含配置类页面。"""
    import actions
    by_attr = {p[0]: p for p in actions.PAGES}
    assert "components_page" in by_attr, "组件预览页未注册进 actions.PAGES"
    assert by_attr["components_page"][3] == "bottom"
    bottom = {p[0] for p in actions.PAGES if p[3] == "bottom"}
    assert {"connect_page", "help_page", "settings_page"} <= bottom
