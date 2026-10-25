# -*- coding: utf-8 -*-
"""R150 交互走查：命令面板真实态截图（输入关键词后的过滤呈现）。"""
import os
import sys
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
    out = ROOT / "output" / "real-palette"
    try:
        w.resize(1280, 820)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        out.mkdir(parents=True, exist_ok=True)
        pal = w._palette
        for tag, q in (("empty", ""), ("tuihui", "退回"), ("reli", "热力图"),
                       ("shenpi", "审批")):
            pal.popup()
            pal.search.setText(q)
            settle(app, max_s=2.0)
            pm = pal.grab()
            pm.save(str(out / f"palette_{tag}.png"))
            print(f"「{q or '(空)'}」→ {pal.listw.count()} 项，已截图")
        pal.close()
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
