# -*- coding: utf-8 -*-
"""临时诊断：截图「接入页」，验证“字看不清”问题。只读不改业务代码。"""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QPA_FONTDIR"] = r"C:\Windows\Fonts"
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont

import ui
from qfluentwidgets import setTheme, Theme

ROOT = r"D:\agent资料\zcode项目记录"
OUT = os.path.join(os.path.dirname(_HERE), "output", "ui_shots")

setTheme(Theme.LIGHT)
app = ui.QApplication(sys.argv)
app.setFont(QFont("Microsoft YaHei UI", 9))
w = ui.AgentHubWindow()
w.resize(1400, 900)
w.show()

if not w.root or not os.path.isdir(w.root):
    w.root = ROOT
    import brain
    brain.init_db(w.root)
    w.refresh()

os.makedirs(OUT, exist_ok=True)


def snap():
    w.switchTo(w.connect_page)
    QTimer.singleShot(1500, grab)  # 等 refresh_status 的 400ms 定时跑完


def grab():
    pm = w.grab()
    p1 = os.path.join(OUT, "connect-LIGHT.png")
    ok = pm.save(p1)
    print(("saved " if ok else "FAILED ") + p1, flush=True)
    # 再截一张深色主题
    setTheme(Theme.DARK)
    w.connect_page.refresh_status()
    QTimer.singleShot(800, grab_dark)


def grab_dark():
    pm = w.grab()
    p2 = os.path.join(OUT, "connect-DARK.png")
    ok = pm.save(p2)
    print(("saved " if ok else "FAILED ") + p2, flush=True)
    QTimer.singleShot(100, app.quit)


QTimer.singleShot(2500, snap)
rc = app.exec()
print("exec rc =", rc, flush=True)
ui._hard_exit(0)
