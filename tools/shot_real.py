# -*- coding: utf-8 -*-
"""临时诊断 3：真实渲染管线（非 offscreen），窗口移到屏幕外截图。用法：python shot_real.py dark|light [page]"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont

import ui
from qfluentwidgets import setTheme, Theme

mode = (sys.argv[1] if len(sys.argv) > 1 else "dark").lower()
page_name = sys.argv[2] if len(sys.argv) > 2 else "connect"
THEME = Theme.DARK if mode == "dark" else Theme.LIGHT

ROOT = r"D:\agent资料\zcode项目记录"
OUT = os.path.join(os.path.dirname(_HERE), "output", "ui_shots")
os.makedirs(OUT, exist_ok=True)

setTheme(THEME)
app = ui.QApplication(sys.argv)
app.setFont(QFont("Microsoft YaHei UI", 9))
w = ui.AgentHubWindow()
w.resize(1400, 900)
w.move(-2600, -2600)  # 屏幕外，不干扰用户
w.show()

if not w.root or not os.path.isdir(w.root):
    w.root = ROOT
    import brain
    brain.init_db(w.root)
    w.refresh()

PAGES = {"connect": lambda: w.connect_page, "overview": lambda: w.overview_page,
         "hub": lambda: w.hub_page}


def snap():
    w.switchTo(PAGES[page_name]())
    QTimer.singleShot(1800, grab)


def grab():
    pm = w.grab()
    p = os.path.join(OUT, f"real-{page_name}-{mode}.png")
    ok = pm.save(p)
    print(("saved " if ok else "FAILED ") + p, flush=True)
    QTimer.singleShot(100, app.quit)


QTimer.singleShot(3000, snap)
rc = app.exec()
print("exec rc =", rc, flush=True)
