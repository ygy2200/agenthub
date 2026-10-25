# -*- coding: utf-8 -*-
"""R137 交互走查：搜索页真实查询态截图（输入关键词→触发→截结果区）。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))

QUERIES = ["署名闸 冒名", "热力图 间距", "备份 恢复", "recency 排序"]


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    real_root = sys.argv[1] if len(sys.argv) > 1 else r"D:\agent资料\zcode项目记录"
    from PySide6.QtWidgets import QApplication
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
    tokens.apply_theme("dark")
    orig = core.get_root
    core.get_root = lambda: real_root
    w = ui.AgentHubWindow()
    out = ROOT / "output" / "real-search"
    try:
        w.resize(1280, 820)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        w.switchTo(w.search_page)
        settle(app, max_s=2.0)
        out.mkdir(parents=True, exist_ok=True)
        for q in QUERIES:
            w.search_page.edit.setText(q)
            w.search_page.run()
            settle(app, max_s=6.0)
            pm = w.search_page.grab()
            pm.save(str(out / f"q_{abs(hash(q)) % 1000}.png"))
            n = w.search_page.resultList.count() if hasattr(w.search_page, "resultList") else -1
            print(f"查询「{q}」→ 截图保存（结果行 {n}）")
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
