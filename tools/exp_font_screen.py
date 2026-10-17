# -*- coding: utf-8 -*-
"""字体修复 A/B 对照（真实屏幕抓取，绕开 Mica 下 grab() 截不到内容的问题）。
用法：python exp_font_screen.py <old|new> <宽> <高>"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QApplication
from qfluentwidgets import setTheme, Theme
from PIL import ImageGrab

import ui

ROOT = r"D:\agent资料\zcode项目记录"
OUT = os.path.abspath(os.path.join(os.path.dirname(_HERE), "output", "ui_shots"))
os.makedirs(OUT, exist_ok=True)

MODE = sys.argv[1] if len(sys.argv) > 1 else "new"
W = int(sys.argv[2]) if len(sys.argv) > 2 else 900
H = int(sys.argv[3]) if len(sys.argv) > 3 else 620

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
if not w.root or not os.path.isdir(w.root):
    w.root = ROOT
    import brain
    brain.init_db(w.root)
    w.refresh()
scr = app.primaryScreen().availableGeometry()
w.resize(W, H)
w.move(scr.x() + (scr.width() - W) // 2, scr.y() + (scr.height() - H) // 2)
w.show()
w.raise_()
w.activateWindow()
w.switchTo(w.connect_page)


def shoot():
    import ctypes
    user32 = ctypes.windll.user32
    user32.SetWindowPos(ctypes.c_void_p(int(w.winId())), ctypes.c_void_p(-1),
                        w.x(), w.y(), w.width(), w.height(), 0x0040)
    QTimer.singleShot(1800, cap)


def cap():
    img = ImageGrab.grab(all_screens=True)
    x, y = w.x(), w.y()
    crop = img.crop((x - 6, y - 6, x + w.width() + 6, y + w.height() + 6))
    p = os.path.join(OUT, f"fs-{MODE}-{W}x{H}.png")
    crop.save(p)
    print(f"saved {p}  DPR={w.devicePixelRatioF()} dpiX={w.logicalDpiX()}", flush=True)
    ui._hard_exit(0)


QTimer.singleShot(3000, shoot)
ui._hard_exit(app.exec())
