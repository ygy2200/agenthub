# -*- coding: utf-8 -*-
"""对照：源码窗口进程加载的模块里有没有 d3dcompiler_47.dll。窗口放屏幕外，零打扰。"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from qfluentwidgets import setTheme, Theme
import ui

setTheme(Theme.DARK)
app = ui.QApplication(sys.argv)
app.setFont(QFont("Microsoft YaHei UI", 9))
w = ui.AgentHubWindow()
w.move(-3200, -3200)
w.show()
w.switchTo(w.connect_page)


def check():
    psapi = ctypes.windll.psapi
    kernel32 = ctypes.windll.kernel32
    pid = kernel32.GetCurrentProcessId()
    hproc = kernel32.GetCurrentProcess()
    mods = (ctypes.c_void_p * 2048)()
    needed = wt.DWORD()
    psapi.EnumProcessModules(ctypes.c_void_p(hproc), ctypes.byref(mods), ctypes.sizeof(mods), ctypes.byref(needed))
    cnt = needed.value // ctypes.sizeof(ctypes.c_void_p)
    names = []
    for i in range(cnt):
        buf = ctypes.create_unicode_buffer(512)
        psapi.GetModuleBaseNameW(ctypes.c_void_p(hproc), ctypes.c_void_p(mods[i]), buf, 512)
        names.append(buf.value)
    print('源码进程模块数:', cnt, flush=True)
    for kw in ('d3d', 'dxcompiler', 'Qt6', 'opengl'):
        hit = [n for n in names if kw.lower() in n.lower()]
        print(f'  [{kw}]:', hit[:10], flush=True)
    os._exit(0)


QTimer.singleShot(3000, check)
rc = app.exec()
