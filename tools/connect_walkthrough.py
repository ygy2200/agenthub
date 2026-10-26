# -*- coding: utf-8 -*-
"""R175 真实接入页走查：汇总行（完成度 + 下一步）与卡片首屏截图。

只读：不点部署/接入/注入，只调用 refresh_status（已由只读守卫覆盖）。
"""
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    real_root = r"D:\agent资料\zcode项目记录"
    from PySide6.QtWidgets import QApplication
    import core
    import tokens
    import ui
    from helpers import pump, settle

    app = QApplication.instance() or QApplication([])
    from PySide6.QtGui import QFontDatabase
    for f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"):
        if os.path.exists(f):
            QFontDatabase.addApplicationFont(f)
            break
    tokens.apply_theme("dark")
    orig = core.get_root
    core.get_root = lambda: real_root
    w = ui.AgentHubWindow()
    out = ROOT / "output" / "real-connect"
    try:
        w.resize(1280, 820)
        w.show()
        w.switchTo(w.connect_page)
        settle(app, max_s=3.0)
        page = w.connect_page
        page.refresh_status()
        settle(app, max_s=1.0)
        out.mkdir(parents=True, exist_ok=True)
        print("汇总：" + page.sumLabel.text())
        tip = page.sumLabel.toolTip().replace("\n", " / ")
        print("细节：" + (tip or "(无待办)"))
        print(f"卡片：{page.rows.count()} 张")
        page.grab().save(str(out / "connect-summary.png"))
        w.resize(920, 600)
        settle(app, max_s=1.0)
        page.grab().save(str(out / "connect-narrow.png"))
        print(f"窄窗 {page.width()}x{page.height()}；截图 → {out}")
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
