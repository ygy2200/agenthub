# -*- coding: utf-8 -*-
"""R806 走查探针：真实库启动窗口，量大脑页「使用分布」每行数值标签的几何与可见性。

截图里条尾看不到数字（只有 probe 行露出一角），但截图会骗人——按 R135 的教训先量控件。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import _console  # noqa: E402,F401  统一 UTF-8 控制台（R222）

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import window  # noqa: E402

REAL = r"D:\agent资料\zcode项目记录"

app = QApplication.instance() or QApplication(sys.argv)
w = window.MainWindow(REAL)
w.resize(1280, 820)
w.show()
w.switchTo(w.brain_page)
for _ in range(30):
    app.processEvents()
w.brain_page.reload()
for _ in range(60):
    app.processEvents()

box = w.brain_page.useBox
print("useBox 宽:", box.geometry().width())
for i in range(box.count()):
    item = box.itemAt(i)
    lay = item.layout()
    if lay is None:
        print(f"[{i}] 非行", item.widget())
        continue
    parts = []
    for j in range(lay.count()):
        wd = lay.itemAt(j).widget()
        if wd is None:
            continue
        g = wd.geometry()
        txt = wd.text() if isinstance(wd, QLabel) else ""
        parts.append(f"{type(wd).__name__}(x={g.x()} w={g.width()} vis={wd.isVisible()} {txt!r})")
    print(f"[{i}]", " | ".join(parts))
print("--- 唤醒排行 ---")
tb = w.brain_page.topBox
print("topBox 宽:", tb.geometry().width())
for i in range(tb.count()):
    wd = tb.itemAt(i).widget()
    if wd is None:
        continue
    g = wd.geometry()
    print(f"[{i}] {type(wd).__name__} x={g.x()} w={g.width()} hint={wd.sizeHint().width()} "
          f"vis={wd.isVisible()}")
    for lab in wd.findChildren(QLabel):
        t = lab.text()
        if not t:
            continue
        lg = lab.geometry()
        fm = lab.fontMetrics()
        need = fm.horizontalAdvance(t)
        print(f"      {type(lab).__name__} '\u2026{t[-14:]}' 宽={lg.width()} 需要={need} "
              f"截断={need > lg.width()}")
w.close()
