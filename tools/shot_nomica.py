# -*- coding: utf-8 -*-
"""临时诊断 2：启动即指定主题（模拟系统深浅色），截图接入页。用法：python shot_connect2.py light|dark"""
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

mode = (sys.argv[1] if len(sys.argv) > 1 else "light").lower()
THEME = Theme.DARK if mode == "dark" else Theme.LIGHT

ROOT = r"D:\agent资料\zcode项目记录"
OUT = os.path.join(os.path.dirname(_HERE), "output", "ui_shots")

setTheme(THEME)  # 创建窗口前设置 = 等价于系统主题就是该值
app = ui.QApplication(sys.argv)
app.setFont(QFont("Microsoft YaHei UI", 9))
w = ui.AgentHubWindow()
w.resize(1400, 900)
w.setMicaEffectEnabled(False)  # 实验：禁用 Mica，改为 qfw 自绘背景
w.show()

if not w.root or not os.path.isdir(w.root):
    w.root = ROOT
    import brain
    brain.init_db(w.root)
    w.refresh()

os.makedirs(OUT, exist_ok=True)


def snap():
    w.switchTo(w.connect_page)
    QTimer.singleShot(1500, grab)


def grab():
    pm = w.grab()
    p = os.path.join(OUT, f"nomica-{mode}.png")
    ok = pm.save(p)
    print(("saved " if ok else "FAILED ") + p, flush=True)
    # 顺带截一页概览做对照
    w.switchTo(w.overview_page)
    QTimer.singleShot(600, grab2)


def grab2():
    pm = w.grab()
    p = os.path.join(OUT, f"overview2-{mode}.png")
    ok = pm.save(p)
    print(("saved " if ok else "FAILED ") + p, flush=True)
    QTimer.singleShot(100, app.quit)


QTimer.singleShot(2500, snap)
rc = app.exec()
print("exec rc =", rc, flush=True)
ui._hard_exit(0)
