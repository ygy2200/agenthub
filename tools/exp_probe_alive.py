# -*- coding: utf-8 -*-
"""鉴别实验 v2：对存活 exe 窗口一次拿到四组数据：
 1) 屏外+材质关：PrintWindow（app 自绘层）
 2) 屏内+材质关+未激活：实机截图
 3) 屏内+材质关+激活：实机截图
 4) 屏内+材质开+激活：实机截图（对照组）
最后移出屏幕。屏幕内总时长约 6 秒，窗口在右下角小区域。"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

OUT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "ui_shots"))
os.makedirs(OUT, exist_ok=True)

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
gdi32 = ctypes.windll.gdi32
user32.FindWindowW.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]

hwnd = user32.FindWindowW(None, "AgentHub")
print("hwnd =", hwnd, flush=True)
if not hwnd:
    print("未找到窗口"); os._exit(1)

H = ctypes.c_void_p(hwnd)
HWND_TOPMOST = ctypes.c_void_p(-1)
HWND_NOTOPMOST = ctypes.c_void_p(-2)

# --- 1) 屏外 + 材质关 + PrintWindow ---
user32.SetWindowPos(H, HWND_NOTOPMOST, -3200, -3200, 1180, 780, 0)
time.sleep(0.8)
dwmapi.DwmSetWindowAttribute(H, 38, ctypes.byref(ctypes.c_int(1)), 4)  # backdrop=NONE
time.sleep(1.0)

rect = wt.RECT()
user32.GetWindowRect(H, ctypes.byref(rect))
W, Hh = rect.right - rect.left, rect.bottom - rect.top
hdc_win = user32.GetWindowDC(H)
hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
hbmp = gdi32.CreateCompatibleBitmap(hdc_win, W, Hh)
gdi32.SelectObject(hdc_mem, hbmp)
rc = user32.PrintWindow(H, hdc_mem, 2)
print("PrintWindow rc =", rc, flush=True)


class BMIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


bmi = BMIH()
bmi.biSize = ctypes.sizeof(BMIH)
bmi.biWidth = W
bmi.biHeight = -Hh
bmi.biPlanes = 1
bmi.biBitCount = 32
buf = ctypes.create_string_buffer(W * Hh * 4)
gdi32.GetDIBits(hdc_mem, hbmp, 0, Hh, buf, ctypes.byref(bmi), 0)

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)
QImage(buf, W, Hh, QImage.Format_RGB32).copy().save(os.path.join(OUT, "v2-1-offscreen-nobackdrop.png"))
print("saved v2-1-offscreen-nobackdrop.png", flush=True)

gdi32.DeleteObject(hbmp); gdi32.DeleteDC(hdc_mem); user32.ReleaseDC(H, hdc_win)

# --- 2) 屏内 + 材质关 + 未激活 ---
user32.SetWindowPos(H, HWND_NOTOPMOST, 1620, 680, 900, 720, 0x40)
time.sleep(1.2)
from PIL import ImageGrab
ImageGrab.grab(all_screens=True).save(os.path.join(OUT, "v2-2-onscreen-nobackdrop.png"))
print("saved v2-2-onscreen-nobackdrop.png", flush=True)

# --- 3) 屏内 + 材质关 + 激活 ---
user32.SetWindowPos(H, HWND_TOPMOST, 1620, 680, 900, 720, 0x40)
user32.SetForegroundWindow(H)
time.sleep(1.2)
ImageGrab.grab(all_screens=True).save(os.path.join(OUT, "v2-3-onscreen-nobackdrop-active.png"))
print("saved v2-3-onscreen-nobackdrop-active.png", flush=True)

# --- 4) 屏内 + 材质开（恢复） + 激活 ---
dwmapi.DwmSetWindowAttribute(H, 38, ctypes.byref(ctypes.c_int(2)), 4)  # Mica 恢复
time.sleep(1.2)
ImageGrab.grab(all_screens=True).save(os.path.join(OUT, "v2-4-onscreen-backdrop.png"))
print("saved v2-4-onscreen-backdrop.png", flush=True)

# --- 收尾：移出屏幕外 ---
user32.SetWindowPos(H, HWND_NOTOPMOST, -3200, -3200, 1180, 780, 0)
print("done", flush=True)
os._exit(0)
