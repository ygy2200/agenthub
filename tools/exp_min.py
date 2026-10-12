# -*- coding: utf-8 -*-
"""实验 A：最小 qfluentwidgets 窗口，真实渲染，深色。python exp_min.py dark|light"""
import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QWidget, QVBoxLayout

from qfluentwidgets import setTheme, Theme, SubtitleLabel, BodyLabel, CardWidget, FluentWindow, NavigationItemPosition

mode = (sys.argv[1] if len(sys.argv) > 1 else "dark").lower()
THEME = Theme.DARK if mode == "dark" else Theme.LIGHT
setTheme(THEME)

app = __import__("PySide6.QtWidgets", fromlist=["QApplication"]).QApplication(sys.argv)
app.setFont(QFont("Microsoft YaHei UI", 9))

w = FluentWindow()
w.resize(900, 600)
w.move(-2600, -2600)
page = QWidget()
lay = QVBoxLayout(page)
lay.addWidget(SubtitleLabel("最小对照窗口 · 深色渲染"))
lay.addWidget(BodyLabel("这是 BodyLabel 普通正文。"))
card = CardWidget()
cl = QVBoxLayout(card)
cl.addWidget(BodyLabel("卡片内文字：若窗口背景正常，这里应该清晰可读。"))
lay.addWidget(card)
page.setObjectName("testPage")
w.addSubInterface(page, None, "测试页")
w.show()


def grab():
    pm = w.grab()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "ui_shots",
                       f"min-{mode}.png")
    out = os.path.abspath(out)
    ok = pm.save(out)
    print(("saved " if ok else "FAILED ") + out, flush=True)
    QTimer.singleShot(100, app.quit)


QTimer.singleShot(1200, grab)
rc = app.exec()
print("exec rc =", rc, flush=True)
