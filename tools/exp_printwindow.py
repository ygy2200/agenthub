# -*- coding: utf-8 -*-
"""实验 D：PrintWindow API 抓窗口内容（窗口放屏幕外，零打扰）。
先对源码窗口测试 PrintWindow 能否捕获 Mica 内容。"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QImage

import ui
from qfluentwidgets import setTheme, Theme

ROOT = r"D:\agent资料\zcode项目记录"
OUT = os.path.abspath(os.path.join(os.path.dirname(_HERE), "output", "ui_shots"))
os.makedirs(OUT, exist_ok=True)

setTheme(Theme.DARK)
app = ui.QApplication(sys.argv)
app.setFont(QFont("Microsoft YaHei UI", 9))
w = ui.AgentHubWindow()
w.resize(1400, 900)
w.move(-2600, -2600)  # 屏幕外
w.show()

if not w.root or not os.path.isdir(w.root):
    w.root = ROOT
    import brain
    brain.init_db(w.root)
    w.refresh()

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def print_window(hwnd, path):
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, width, height)
    gdi32.SelectObject(hdc_mem, hbmp)
    # PW_RENDERFULLCONTENT = 2
    ok = user32.PrintWindow(hwnd, hdc_mem, 2)
    print(f"PrintWindow rc={ok} size={width}x{height}", flush=True)

    # 取出 bitmap 数据
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = width
    bmi.biHeight = -height  # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bufsize = width * height * 4
    buf = ctypes.create_string_buffer(bufsize)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), 0)
    img = QImage(buf, width, height, QImage.Format_RGB32).copy()
    img.save(path)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_win)
    print("saved", path, flush=True)


def snap():
    w.switchTo(w.connect_page)
    QTimer.singleShot(1500, grab)


def grab():
    hwnd = int(w.winId())
    print("hwnd =", hwnd, flush=True)
    print_window(hwnd, os.path.join(OUT, "pw-dark-mica.png"))
    QTimer.singleShot(100, app.quit)


QTimer.singleShot(2500, snap)
rc = app.exec()
print("exec rc =", rc, flush=True)
ui._hard_exit(0)
