# -*- coding: utf-8 -*-
"""真实库走查：连真实 root 启动窗口逐页截图（只读，不种子不改库）。
用法：python tools/real_walkthrough.py [root] [dark|light] [输出目录后缀]"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))

THEME = sys.argv[2] if len(sys.argv) > 2 else "dark"
SUFFIX = sys.argv[3] if len(sys.argv) > 3 else ""
OUT = ROOT / "output" / f"real-shots{SUFFIX}"


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    real_root = sys.argv[1] if len(sys.argv) > 1 else r"D:gent资料\zcode项目记录"
    from PySide6.QtWidgets import QApplication
    import actions
    import core
    import tokens
    import ui
    from helpers import pump, settle

    app = QApplication.instance() or QApplication([])
    for f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"):
        if os.path.exists(f):
            from PySide6.QtGui import QFontDatabase
            QFontDatabase.addApplicationFont(f)
            break
    tokens.apply_theme(THEME)
    orig = core.get_root
    core.get_root = lambda: real_root
    w = ui.AgentHubWindow()
    try:
        w.resize(1280, 820)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        pump(app, lambda: bool(getattr(w.hub_page, "agents", None)), 8.0)
        OUT.mkdir(parents=True, exist_ok=True)
        for attr, title, *_ in actions.PAGES:
            page = getattr(w, attr, None)
            if page is None:
                continue
            w.switchTo(page)
            settle(app, max_s=3.0)
            pm = page.grab()
            if not pm.isNull():
                pm.save(str(OUT / f"{attr}.png"))
        print(f"[{THEME}] 已截图 -> {OUT}")
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
