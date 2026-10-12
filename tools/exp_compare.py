# -*- coding: utf-8 -*-
"""实验 G：对照组——exe 窗口(右下) vs 源码窗口(左下) 并排截图，一次锁定渲染差异。
窗口显示约 4 秒后全部关闭（源码窗口用 TerminateProcess 退出，不触发 closeEvent 污染配置）。"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

EXE = r"C:\Users\y\Desktop\AgentHub.exe"
OUT = os.path.abspath(os.path.join(os.path.dirname(_HERE), "output", "ui_shots"))
os.makedirs(OUT, exist_ok=True)

user32 = ctypes.windll.user32
user32.FindWindowW.restype = ctypes.c_void_p
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]

HWND_TOPMOST = -1
SWP_SHOWWINDOW = 0x0040

# ---- 1) 启动 exe 并等窗口 ----
print("启动 exe ...", flush=True)
proc = subprocess.Popen([EXE])
ehwnd = None
for i in range(80):
    ehwnd = user32.FindWindowW(None, "AgentHub")
    if ehwnd:
        break
    time.sleep(0.5)
if not ehwnd:
    print("FAIL: 未找到 exe 窗口", flush=True)
    proc.terminate(); sys.exit(1)
print("exe 窗口出现:", ehwnd, flush=True)

# ---- 2) exe 窗口放右下角 + 置顶 ----
user32.SetWindowPos(ctypes.c_void_p(ehwnd), ctypes.c_void_p(HWND_TOPMOST),
                    1640, 700, 900, 700, SWP_SHOWWINDOW)
time.sleep(0.8)

# ---- 3) 源码窗口 —— 单独用一个 QProcess 风格的子进程跑，避免同进程干扰？ 简化：同进程创建 ----
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from qfluentwidgets import setTheme, Theme
import ui

setTheme(Theme.DARK)
app = ui.QApplication(sys.argv)
app.setFont(QFont("Microsoft YaHei UI", 9))
w = ui.AgentHubWindow()
w.resize(880, 700)
w.move(20, 700)
w.show()
user32.SetWindowPos(ctypes.c_void_p(int(w.winId())), ctypes.c_void_p(HWND_TOPMOST),
                    20, 700, 880, 700, SWP_SHOWWINDOW)
w.switchTo(w.connect_page)  # 源码窗口也显示接入页
print("源码窗口已显示", flush=True)


def shoot():
    from PIL import ImageGrab
    img = ImageGrab.grab(all_screens=True)
    img.save(os.path.join(OUT, "compare-full.png"))
    print("saved compare-full.png", flush=True)
    # 关闭 exe：直接硬杀（TerminateProcess），绝不走 closeEvent，避免写 config.json
    proc.kill()
    print("exe 已硬杀", flush=True)
    # 源码窗口直接硬退（不触发 closeEvent 保存几何）
    os._exit(0)


QTimer.singleShot(2200, shoot)
rc = app.exec()
os._exit(0)
