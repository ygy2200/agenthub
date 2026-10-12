# -*- coding: utf-8 -*-
"""实验 C：真实屏幕截图（GDI 全屏），看用户实际所见。窗口显示约 4 秒后自动关闭。
用法：python exp_screen.py [dark|light]"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont

import ui
from qfluentwidgets import setTheme, Theme

mode = (sys.argv[1] if len(sys.argv) > 1 else "dark").lower()
THEME = Theme.DARK if mode == "dark" else Theme.LIGHT

ROOT = r"D:\agent资料\zcode项目记录"
OUT = os.path.abspath(os.path.join(os.path.dirname(_HERE), "output", "ui_shots"))
os.makedirs(OUT, exist_ok=True)

setTheme(THEME)
app = ui.QApplication(sys.argv)
app.setFont(QFont("Microsoft YaHei UI", 9))
w = ui.AgentHubWindow()
w.resize(1400, 900)

# 屏幕中央
scr = app.primaryScreen().availableGeometry()
w.move(scr.x() + (scr.width() - 1400) // 2, scr.y() + (scr.height() - 900) // 2)
w.show()

if not w.root or not os.path.isdir(w.root):
    w.root = ROOT
    import brain
    brain.init_db(w.root)
    w.refresh()


def stage1():
    w.switchTo(w.connect_page)
    QTimer.singleShot(1500, shoot)


def shoot():
    from PIL import ImageGrab
    img = ImageGrab.grab(all_screens=True)
    # 裁剪窗口区域（窗口坐标 + 边框余量）
    x, y = w.x(), w.y()
    box = (x - 8, y - 40, x + w.width() + 8, y + w.height() + 8)
    img.crop(box).save(os.path.join(OUT, f"screen-{mode}-mica.png"))
    print(f"saved screen-{mode}-mica.png", flush=True)
    # 第二阶段：关闭 Mica，再截一次
    w.setMicaEffectEnabled(False)
    QTimer.singleShot(1200, shoot2)


def shoot2():
    from PIL import ImageGrab
    img = ImageGrab.grab(all_screens=True)
    x, y = w.x(), w.y()
    box = (x - 8, y - 40, x + w.width() + 8, y + w.height() + 8)
    img.crop(box).save(os.path.join(OUT, f"screen-{mode}-nomica.png"))
    print(f"saved screen-{mode}-nomica.png", flush=True)
    # 第三阶段：禁用 Mica + 强制刷新背景后，恢复 Mica 再截
    w.setMicaEffectEnabled(True)
    QTimer.singleShot(1200, shoot3)


def shoot3():
    from PIL import ImageGrab
    img = ImageGrab.grab(all_screens=True)
    x, y = w.x(), w.y()
    box = (x - 8, y - 40, x + w.width() + 8, y + w.height() + 8)
    img.crop(box).save(os.path.join(OUT, f"screen-{mode}-mica2.png"))
    print(f"saved screen-{mode}-mica2.png", flush=True)
    QTimer.singleShot(200, app.quit)


QTimer.singleShot(2200, stage1)
rc = app.exec()
print("exec rc =", rc, flush=True)
