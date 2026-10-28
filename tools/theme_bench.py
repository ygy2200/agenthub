# -*- coding: utf-8 -*-
"""R223 主题切换拆账：一次切主题的钱到底花在哪一段。

背景：真实窗口切浅/深主题实测 1.5–1.9s，用户能明显感到"卡一下"。此前只知
qfw 逐控件 setStyleSheet，不知道 **哪一段** 占大头，也没量过 qfw 自带的
`lazy=True`（只刷可见控件，隐藏的标 dirty 等首次绘制再刷）能省多少。

量的是四段（各自独立计时，可加总核对）：
  ① setTheme(lazy=False)  qfw 全量刷新
  ② setTheme(lazy=True)   qfw 只刷可见
  ③ refresh_token_styles  我们自己的 token 绑定样式（uikit.bind_style 登记）
  ④ _apply_page_bgs + apply_accent  页面底色与主色

用法：python tools/theme_bench.py [--root <大脑根>]
"""
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R222）
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _pump(app, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.002)


def _time(fn, repeat=3):
    best = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return best


def main(argv) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    root = r"D:\agent资料\zcode项目记录"
    if "--root" in argv:
        root = argv[argv.index("--root") + 1]
    from PySide6.QtWidgets import QApplication
    import core
    import tokens
    import uikit
    import window as win_mod
    import ui

    app = QApplication.instance() or QApplication([])
    tokens.apply_theme("dark")
    core.get_root = lambda: root
    w = ui.AgentHubWindow()
    w.show()
    _pump(app, 0.5)
    deadline = time.perf_counter() + 20
    while time.perf_counter() < deadline and not getattr(w, "_apply_count", 0):
        _pump(app, 0.05)
    _pump(app, 0.4)

    from qfluentwidgets.common.style_sheet import styleSheetManager
    widgets = list(styleSheetManager.widgets)
    visible = sum(1 for x in widgets if not x.visibleRegion().isNull())
    bound = [r() for r, _ in uikit._STYLE_BOUND]
    bound_alive = []
    for x in bound:
        if x is None:
            continue
        try:
            if x.isVisible() is not None:
                bound_alive.append(x)
        except RuntimeError:  # 死包装：refresh_token_styles 下一轮会剔掉
            continue
    bound_vis = sum(1 for x in bound_alive if x.isVisible())
    print(f"qfw 注册控件 {len(widgets)}（可见 {visible}）")
    print(f"token 绑定控件 {len(bound_alive)}（可见 {bound_vis}）")

    from qfluentwidgets import Theme, isDarkTheme, qconfig, setTheme

    def reset_dark():
        qconfig.theme = Theme.DARK
        setTheme(Theme.DARK, save=False, lazy=True)

    rows = {}
    for mode in (False, True):
        def go(mode=mode):
            target = Theme.LIGHT if qconfig.theme == Theme.DARK else Theme.DARK
            setTheme(target, save=False, lazy=mode)
        reset_dark()
        rows[f"setTheme(lazy={mode})"] = _time(go)

    qconfig.theme = Theme.DARK
    rows["refresh_token_styles"] = _time(uikit.refresh_token_styles)
    rows["_apply_page_bgs"] = _time(w._apply_page_bgs)
    rows["apply_accent"] = _time(tokens.apply_accent)

    def full_switch():
        tokens.apply_theme("light" if isDarkTheme() else "dark")

    rows["apply_theme 整体（现状）"] = _time(full_switch, repeat=2)

    for k, v in rows.items():
        print(f"  {k}: {v * 1000:.0f}ms")

    # 细账：qfw 一次刷新内部到底把时间花在「取 qss 文本」还是「给每个控件上样式」
    import qfluentwidgets.common.style_sheet as ss
    orig_set, orig_get = ss.setStyleSheet, ss.getStyleSheet
    acc = {"set_n": 0, "set_t": 0.0, "get_n": 0, "get_t": 0.0, "set_hidden": 0}

    def timed_set(widget, source, theme=None):
        t0 = time.perf_counter()
        try:
            return orig_set(widget, source, theme)
        finally:
            acc["set_n"] += 1
            acc["set_t"] += time.perf_counter() - t0
            try:
                if widget.visibleRegion().isNull():
                    acc["set_hidden"] += 1
            except Exception:  # noqa: BLE001 死包装只影响统计
                pass

    def timed_get(source, theme=None):
        t0 = time.perf_counter()
        try:
            return orig_get(source, theme)
        finally:
            acc["get_n"] += 1
            acc["get_t"] += time.perf_counter() - t0

    ss.setStyleSheet, ss.getStyleSheet = timed_set, timed_get
    try:
        reset_dark()

        def go_full():
            target = Theme.LIGHT if qconfig.theme == Theme.DARK else Theme.DARK
            setTheme(target, save=False, lazy=False)
        for k in acc:
            acc[k] = 0 if isinstance(acc[k], int) else 0.0
        full = _time(go_full, repeat=1)
    finally:
        ss.setStyleSheet, ss.getStyleSheet = orig_set, orig_get
    print(f"  ├ setStyleSheet 调用 {acc['set_n']} 次（其中隐藏控件 {acc['set_hidden']}）"
          f" 累计 {acc['set_t'] * 1000:.0f}ms")
    print(f"  ├ getStyleSheet 调用 {acc['get_n']} 次 累计 {acc['get_t'] * 1000:.0f}ms")
    print(f"  └ 整次 lazy=False {full * 1000:.0f}ms（其余为信号/布局/重绘）")

    print(f"合计（阶段相加）：{sum(rows.values()) * 1000:.0f}ms（整体含重复段，勿直接比）")
    w._skip_close_hooks = True
    w.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
