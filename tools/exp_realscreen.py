# -*- coding: utf-8 -*-
"""实验 E：实机像素级验证（GDI 全屏截图），窗口在屏幕上只出现约 2.5 秒。
流程：建窗(深色) -> 居中显示 -> 2.5s 后全屏截图 -> 立即移出屏幕 -> 保存 -> 退出。"""
import os
import sys

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
scr = app.primaryScreen().availableGeometry()
win_pos = (scr.x() + (scr.width() - 1400) // 2, scr.y() + (scr.height() - 900) // 2)
w.move(*win_pos)

if not w.root or not os.path.isdir(w.root):
    w.root = ROOT
    import brain
    brain.init_db(w.root)
    w.refresh()


def show_and_wait():
    w.show()
    w.raise_()
    w.activateWindow()
    # 2.5 秒后截图（用户可见时间 ≈ 2.5s）
    QTimer.singleShot(2500, shoot)


def shoot():
    from PIL import ImageGrab
    img = ImageGrab.grab(all_screens=True)
    img.save(os.path.join(OUT, f"realscreen-{mode}-full.png"))
    print("full screen size:", img.size, flush=True)
    # 立刻移出屏幕 + 关闭 → 结束打扰
    w.move(-4000, -4000)
    w.hide()
    w.close()
    QTimer.singleShot(150, app.quit)


# 切到接入页后再显示（接入页是用户报障页）
def prep():
    w.switchTo(w.connect_page)
    QTimer.singleShot(300, show_and_wait)


QTimer.singleShot(2000, prep)
rc = app.exec()
print("exec rc =", rc, "win_pos:", win_pos, flush=True)
ui._hard_exit(0)
