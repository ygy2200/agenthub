# -*- coding: utf-8 -*-
"""R775：盘插回来之后，错误提示与设置页状态都要跟着恢复。

R772 的「根目录不存在」横幅只在换根目录（set_root）时撤；用户把移动硬盘插回、
按 F5 让扫描成功后，横幅仍挂着——错误提示比故障活得久，且设置页状态行停在
「目录不可用」（apply 不重载设置页），人看着两处都在报一个已经不存在的病。
"""
import os

from helpers import pump, settle


def _boot_dead(qapp, monkeypatch, path):
    import core
    import ui
    monkeypatch.setattr(core, "get_root", lambda: str(path))
    w = ui.AgentHubWindow()
    w._skip_close_hooks = True
    w.resize(1100, 760)
    w.show()
    return w


def test_recovered_root_clears_banner_and_settings_state(qapp, hub_root, monkeypatch, tmp_path):
    dead = tmp_path / "drive_comes_back"
    w = _boot_dead(qapp, monkeypatch, dead)
    try:
        assert pump(qapp, lambda: getattr(w, "_root_bar", None) is not None, 10)
        assert pump(qapp, lambda: w._apply_count >= 1, 15)
        w.switchTo(w.settings_page)
        settle(qapp, max_s=1.0)
        assert "目录不可用" in w.settings_page.rootState.text()

        os.makedirs(dead)
        before = w._apply_count
        w.refresh()
        assert pump(qapp, lambda: w._apply_count > before, 20), "恢复后的刷新没有落地"
        assert w._root_bar is None, "目录已恢复，旧的「根目录不存在」横幅还挂着"
        # 用户就在设置页按的 F5：状态行必须原地转好，不能等切页才刷新
        assert "目录可用" in w.settings_page.rootState.text(), \
            w.settings_page.rootState.text()
    finally:
        w._skip_close_hooks = True
        w.close()


def test_dead_root_banner_points_at_f5_retry(qapp, hub_root, monkeypatch):
    dead = os.path.join(os.path.dirname(hub_root), "still_missing")
    w = _boot_dead(qapp, monkeypatch, dead)
    try:
        assert pump(qapp, lambda: getattr(w, "_root_bar", None) is not None, 10)
        blob = w._root_bar.content or ""
        assert "F5" in blob, f"盘插回后的恢复动作没写出来：{blob}"
    finally:
        w._skip_close_hooks = True
        w.close()
