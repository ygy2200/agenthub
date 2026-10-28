# -*- coding: utf-8 -*-
"""R240：离屏实例退出时不写用户配置（把 R232 的闸门补全）。

R232 定了「诊断/测试工具不得写 `~/.agenthub/config.json`」，兜底判据却只有
`PYTEST_CURRENT_TEST`——**不带 pytest 的离屏脚本照样能写**。本轮就踩实了：
`output/_r240_subcost.py` 建窗口后调 `win.close()`，用户配置
md5 从 `3700455b…` 变成 `35e0f1eb…`，`win_geometry` 被离屏窗口的 1200×800 覆盖，
用户下次开应用的窗口尺寸被我改掉（旧值无备份，不可恢复）。

正解不是「以后脚本记得设 `_skip_close_hooks`」（同一根因第二次犯），而是把判据
改成**这个实例不是用户正在用的桌面会话**：离屏（`QT_QPA_PLATFORM=offscreen`）没有
真实桌面窗口，它的几何与导航态本来就不该进用户配置。判据抽成纯函数，整张表可单测；
`closeEvent` 的接线在 `tests/gui/test_offscreen_close_quiet_r240.py` 里单独验。
"""
import window as window_mod


def test_offscreen_instance_is_quiet():
    """离屏＝静默：不备份大脑、不写配置、不排 2s 硬退定时器。"""
    assert window_mod._close_hooks_quiet({"QT_QPA_PLATFORM": "offscreen"}, False) is True


def test_pytest_process_is_quiet_even_without_offscreen_var():
    """R232 那条兜底照旧成立（PYTEST_CURRENT_TEST 由 pytest 注入并被子进程继承）。"""
    assert window_mod._close_hooks_quiet({"PYTEST_CURRENT_TEST": "tests/gui/test_x.py::t"},
                                         False) is True


def test_explicit_skip_flag_is_quiet():
    assert window_mod._close_hooks_quiet({}, True) is True


def test_desktop_session_is_NOT_quiet():
    """反向闸：生产桌面会话必须照常记住窗口尺寸与导航折叠态。

    少了这条，「静默」会被写成「永远静默」——用户下次开应用窗口位置每次都回到默认，
    那是比本轮事故更常被察觉的回归。
    """
    assert window_mod._close_hooks_quiet({"QT_QPA_PLATFORM": "windows"}, False) is False
    assert window_mod._close_hooks_quiet({}, False) is False


def test_other_qt_platforms_do_not_become_quiet_by_accident():
    """只有 offscreen 这一档算「非桌面会话」；minimal/vnc 等照旧按生产处理。"""
    for plat in ("minimal", "wayland", "", None):
        env = {} if plat is None else {"QT_QPA_PLATFORM": plat}
        assert window_mod._close_hooks_quiet(env, False) is False, plat
