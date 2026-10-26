# -*- coding: utf-8 -*-
"""R145 性能走查：真实库下各页 reload 计时（>2s 即体验问题）。"""
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    real_root = r"D:\agent资料\zcode项目记录"
    from PySide6.QtWidgets import QApplication
    import actions
    import core
    import tokens
    import ui
    from helpers import pump

    app = QApplication.instance() or QApplication([])
    tokens.apply_theme("dark")
    orig = core.get_root
    core.get_root = lambda: real_root
    t0 = time.perf_counter()
    w = ui.AgentHubWindow()
    init = time.perf_counter() - t0
    print(f"窗口初始化（含首次 refresh）：{init:.2f}s")
    pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
    rows = []
    for attr, title, *_ in actions.PAGES:
        page = getattr(w, attr, None)
        if page is None:
            continue
        w.switchTo(page)
        t = time.perf_counter()
        try:
            page.reload()
        except Exception as e:  # noqa: BLE001
            rows.append((title, -1, f"{type(e).__name__}: {e}"))
            continue
        rows.append((title, time.perf_counter() - t, ""))
    for title, sec, err in sorted(rows, key=lambda x: -x[1]):
        flag = " ⚠慢" if sec > 2 else ""
        print(f"  {title}: {sec:.2f}s{flag} {err}")
    w.close()


if __name__ == "__main__":
    main()
