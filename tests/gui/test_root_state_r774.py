# -*- coding: utf-8 -*-
"""R774：设置页根目录卡片自己说清状态（可用 / 不可用 / 未初始化）。

R771 实测「盘不在 → 总览显示空库」，R772 用窗口横幅兜住了启动那一刻；
但设置页——用户真正去改目录的地方——此前只显示路径本身。横幅一关，
这一页看不出目录已经坏了（对比 Lightroom：外置盘未挂载时给
「Find Missing Folder…」的恢复入口，而不是静默当空目录）。
"""
import os

from helpers import boot_window, dead_path, settle


def _settings(qapp, root):
    import actions
    w = boot_window(qapp, root)
    page = actions.page_of(w, "settings_page")
    w.switchTo(page)
    settle(qapp, max_s=1.0)
    return w, page


def test_dead_root_state_is_explicit(qapp, hub_root, tmp_path):
    w, page = _settings(qapp, hub_root)
    dead = dead_path("gone_r774")
    assert not os.path.isdir(dead)
    w.root = dead
    page.reload()
    text = page.rootState.text()
    assert "目录不可用" in text and "选择" in text, text
    assert page._root_tone == "warning"
    assert "color:" in page.rootState.styleSheet(), "不可用状态必须与普通说明文案有视觉区分"


def test_available_root_state_and_uninitialized_hint(qapp, hub_root, tmp_path):
    import core
    w, page = _settings(qapp, hub_root)
    fresh = tmp_path / "fresh-root"
    fresh.mkdir()
    w.root = str(fresh)
    page.reload()
    assert "目录可用" in page.rootState.text()
    assert "尚未初始化" in page.rootState.text(), "可用但没建过库时要给下一步，而不是只说可用"
    assert page._root_tone == "success"
    (fresh / core.DIR_META).mkdir()
    page.reload()
    assert "大脑已初始化" in page.rootState.text()
    w.root = ""
    page.reload()
    assert "还没有选择根目录" in page.rootState.text()
    assert page.rootState.styleSheet() == "", "中性提示不该沿用上一个状态的染色"
