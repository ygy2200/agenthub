# -*- coding: utf-8 -*-
"""R772：根目录不存在时，界面不许把它显示成一个空库。

R771 的开机走查实测：配置里的根目录所在盘不在（盘符变了/移动硬盘没插）时，
窗口既不建库也不扫描，总览页显示「项目总数 0」——用户以为库空了，实际是盘没了。
同 R762「读失败被当成空库」一族，只是失败发生在更早的启动阶段。

口径：启动即给一条持久横幅（含路径 + 可能原因 + 「设置」页下一步），且可关闭；
换到可用根目录后横幅自动消失。
"""
import os

from helpers import boot_window, dead_path, pump, settle


def test_dead_root_shows_persistent_banner(qapp, hub_root, monkeypatch):
    import core
    import ui

    dead = dead_path("missing_r772")
    assert not os.path.isdir(dead)
    monkeypatch.setattr(core, "get_root", lambda: dead)
    w = ui.AgentHubWindow()
    w._skip_close_hooks = True
    w.resize(1100, 760)
    w.show()
    try:
        assert pump(qapp, lambda: getattr(w, "_root_bar", None) is not None, 10), \
            "根目录不存在时没有给任何提示"
        bar = w._root_bar
        assert "根目录不存在" in bar.title
        blob = (bar.content or "")
        assert dead in blob and "设置" in blob and "移动硬盘" in blob, blob
        assert bar.duration < 0, "盘不在不会自愈，提示不许自动消失"
        assert bar.isVisible(), "横幅没有上屏"
        # R772 第二处缺陷：首刷会从 journal_page.reload 抛 sqlite 错误、把 apply 掐断，
        # 后续页不更新且 _apply_count 不涨。这里钉住「整次刷新真的走完了」。
        assert pump(qapp, lambda: w._apply_count >= 1, 15), \
            "根目录不存在时首刷被中途异常掐断（apply 没走完）"
    finally:
        w._skip_close_hooks = True
        w.close()


def test_banner_cleared_after_picking_valid_root(qapp, hub_root, monkeypatch):
    import core
    import ui

    dead = dead_path("missing_r772b")
    monkeypatch.setattr(core, "get_root", lambda: dead)
    # 真实 set_root 会因为 tmp_path 命中「Temp 写入全局配置」守卫而拒绝（R296），
    # 本用例只验窗口层行为，写配置这步由既有用例覆盖
    monkeypatch.setattr(core, "set_root", lambda d: None)
    w = ui.AgentHubWindow()
    w._skip_close_hooks = True
    w.resize(1100, 760)
    w.show()
    try:
        assert pump(qapp, lambda: getattr(w, "_root_bar", None) is not None, 10)
        w.set_root(hub_root)
        settle(qapp, max_s=2.0)
        assert w.root == hub_root
        assert w._root_bar is None, "换到可用根目录后旧横幅还挂着"
    finally:
        w._skip_close_hooks = True
        w.close()
