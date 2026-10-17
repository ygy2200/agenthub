# -*- coding: utf-8 -*-
"""字体修复 A/B 对照：同窗口尺寸、旧字体设置 vs 新字体设置，抓图放大对比。
重点复现用户症状：缩小窗口时字形被裁。"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QApplication
from qfluentwidgets import setTheme, Theme

import ui

ROOT = r"D:\agent资料\zcode项目记录"
OUT = os.path.abspath(os.path.join(os.path.dirname(_HERE), "output", "ui_shots"))
os.makedirs(OUT, exist_ok=True)

MODE = sys.argv[1] if len(sys.argv) > 1 else "new"   # old | new
SIZE = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (1000, 700)

if MODE == "new":
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
app = QApplication(sys.argv)
setTheme(Theme.DARK)

if MODE == "new":
    ui._setup_app_font(app)
else:
    app.setFont(QFont("Microsoft YaHei UI", 9))

w = ui.AgentHubWindow()
w.resize(*SIZE)
w.move(400, 150)
if not w.root or not os.path.isdir(w.root):
    w.root = ROOT
    import brain
    brain.init_db(w.root)
    w.refresh()
w.show()
w.switchTo(w.connect_page)


def shoot():
    pm = w.grab()
    p = os.path.join(OUT, f"font-{MODE}-{SIZE[0]}x{SIZE[1]}.png")
    ok = pm.save(p)
    print(("saved " if ok else "FAILED ") + p, flush=True)
    print(f"DPR={w.devicePixelRatioF()} logicalDpi={w.logicalDpi()}", flush=True)
    ui._hard_exit(0)


QTimer.singleShot(3500, shoot)
ui._hard_exit(app.exec())
