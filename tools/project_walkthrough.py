# -*- coding: utf-8 -*-
"""项目页真实库截图（R168）：长记录列表 + 下拉跳到最后一条。

用法：python tools/project_walkthrough.py [root] [输出后缀]
产出：output/real-project<后缀>/records.png、jump-last.png
"""
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
    real_root = sys.argv[1] if len(sys.argv) > 1 else r"D:\agent资料\zcode项目记录"
    suffix = sys.argv[2] if len(sys.argv) > 2 else ""
    from PySide6.QtWidgets import QApplication
    import brain
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
    out = ROOT / "output" / f"real-project{suffix}"
    try:
        w.resize(1280, 820)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        w.switchTo(w.project_page)
        pp = w.project_page
        settle(app, max_s=2.0)
        out.mkdir(parents=True, exist_ok=True)

        projects = brain.list_projects(real_root, 200)
        target = max(projects, key=lambda p: p.get("n_records", 0))
        name = target["name"]
        rows = [pp.listw.item(i).text() for i in range(pp.listw.count())]
        idx = next(i for i, t in enumerate(rows)
                   if t.rsplit("   (", 1)[0] == name)
        t0 = time.time()
        pp.listw.setCurrentRow(idx)
        settle(app, max_s=2.0)
        elapsed = time.time() - t0
        sb = pp.browser.verticalScrollBar()
        text = pp.browser.toPlainText()
        print(f"项目「{name}」：{target.get('n_records')} 条记录 → 浏览器正文 {len(text)} 字，"
              f"下拉 {pp.recCombo.count()} 项，滚动上限 {sb.maximum()}，渲染 {elapsed:.2f}s",
              flush=True)
        print(f"计数标签「{pp.recCount.text()}」；首条「{pp.recCombo.itemText(0)}」",
              flush=True)
        pp.grab().save(str(out / "records.png"))

        if pp.recCombo.count():
            last = pp.recCombo.count() - 1
            pp.jump_record(last)
            settle(app, max_s=1.5)
            print(f"跳到最后一条「{pp.recCombo.itemText(last)}」→ 滚动 {sb.value()}/{sb.maximum()}",
                  flush=True)
            pp.grab().save(str(out / "jump-last.png"))
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
