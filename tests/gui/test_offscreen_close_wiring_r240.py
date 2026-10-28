# -*- coding: utf-8 -*-
"""R240：诊断/测试实例关窗不许写用户状态——接线核对（纯判据表在 tests/unit/test_offscreen_close_quiet_r240.py）。

这里只验判据表验不了的事：`closeEvent` 真的用 `window._close_hooks_quiet`，而不是留在
R232 的旧表达式上。两条判据（离屏 / pytest）各拆一条环境变量单独点亮，避免「两条一起
亮」的假绿。离屏那条必须先把 `PYTEST_CURRENT_TEST` 从环境摘掉——否则静默是旧兜底给的，
改回旧代码测试照样绿。

事故本体：`output/_r240_subcost.py`（不经 pytest 的 offscreen 探针）建窗后调
`win.close()`，把用户 `~/.agenthub/config.json` 的 `win_geometry` 覆盖成离屏尺寸，
md5 从 `3700455b…` 变 `35e0f1eb…`，旧值无备份不可恢复。
"""


def _close_without_flag(monkeypatch, hub_root):
    """建一个**没设** `_skip_close_hooks` 的窗口并关闭，返回 (save_config 调用, backup 调用)。

    调用方负责先把环境调成要验的那一支；这里只负责「不靠显式标记，纯靠环境判据」。
    """
    import brain
    import core
    import ui
    from helpers import _WINDOWS

    saved, backed = [], []
    monkeypatch.setattr(core, "save_config", lambda *a, **k: saved.append(a))
    monkeypatch.setattr(core, "load_config", lambda: {"root": hub_root})
    monkeypatch.setattr(brain, "backup_brain", lambda *a, **k: backed.append(a))

    w = ui.AgentHubWindow()
    w._skip_close_hooks = False  # 关键：不许靠显式标记，只能靠环境判据本身
    w.resize(1200, 800)
    w.show()
    _WINDOWS.append(w)
    w.close()
    return saved, backed


def test_offscreen_close_writes_no_user_state(qapp, hub_root, monkeypatch):
    """离屏关窗：不存配置、不备份大脑（用户状态与真实库都不该被诊断实例改动）。"""
    import os

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    assert os.environ.get("QT_QPA_PLATFORM") == "offscreen"

    saved, backed = _close_without_flag(monkeypatch, hub_root)
    assert saved == [], "离屏窗口关闭仍写了用户配置：%s" % saved
    assert backed == [], "离屏窗口关闭仍备份了真实大脑：%s" % backed


def test_pytest_env_close_writes_no_user_state(qapp, hub_root, monkeypatch):
    """R232 那条 pytest 兜底义务的**行为**版本（原判据是源码字面量，R240 抽函数后搬家）。

    这里把离屏变量摘掉，只留 `PYTEST_CURRENT_TEST`——两条判据分成各能独立生效，
    摘掉哪条就只剩另一条能救，缺一条就会红（不是「两条一起亮」的假绿）。
    """
    import os

    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/gui/x.py::test_y")
    assert os.environ.get("QT_QPA_PLATFORM") is None

    saved, backed = _close_without_flag(monkeypatch, hub_root)
    assert saved == [], "pytest 环境下的窗口关闭写了用户配置：%s" % saved
    assert backed == [], "pytest 环境下的窗口关闭备份了真实大脑：%s" % backed
