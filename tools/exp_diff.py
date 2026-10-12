# -*- coding: utf-8 -*-
"""差分实验：窗口在屏 vs 移到屏外，两张全屏截图的差异 = 窗口的真实可见区域。"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

EXE = r"C:\Users\y\Desktop\AgentHub.exe"
OUT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "ui_shots"))
os.makedirs(OUT, exist_ok=True)

user32 = ctypes.windll.user32
user32.FindWindowW.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]

subprocess.run(["taskkill", "/F", "/T", "/IM", "AgentHub.exe"], capture_output=True)
time.sleep(0.8)
print("启动 exe（自然）...", flush=True)
proc = subprocess.Popen([EXE])
hwnd = None
for i in range(40):
    hwnd = user32.FindWindowW(None, "AgentHub")
    if hwnd:
        break
    time.sleep(0.5)
print("hwnd:", hwnd, flush=True)
time.sleep(5.0)  # 等渲染稳定

from PIL import ImageGrab, ImageChops
H = ctypes.c_void_p(hwnd)

# 截图 A：窗口在屏
A = ImageGrab.grab(all_screens=True)
A.save(os.path.join(OUT, "diff-A-onscreen.png"))
print("A saved", flush=True)

# 移到屏幕外
user32.SetWindowPos(H, ctypes.c_void_p(-2), -3200, -3200, 1180, 780, 0)
time.sleep(1.5)
B = ImageGrab.grab(all_screens=True)
B.save(os.path.join(OUT, "diff-B-offscreen.png"))
print("B saved", flush=True)

# 差分
diff = ImageChops.difference(A.convert("RGB"), B.convert("RGB"))
bbox = diff.getbbox()
print("差异区域 bbox:", bbox, flush=True)
if bbox:
    # 统计差异强度
    g = diff.convert("L")
    hist = g.histogram()
    total = sum(hist)
    strong = sum(hist[15:])  # 差异 >15 的像素数
    print(f"差异像素（>15）: {strong} / {total} = {100*strong/total:.2f}%", flush=True)
    diff.crop(bbox).save(os.path.join(OUT, "diff-crop.png"))
    # 差异区域的可视化增强
    from PIL import ImageOps
    d2 = ImageOps.autocontrast(diff.crop(bbox), cutoff=0)
    d2.save(os.path.join(OUT, "diff-crop-enhanced.png"))
    print("diff 图已存", flush=True)

subprocess.run(["taskkill", "/F", "/T", "/IM", "AgentHub.exe"], capture_output=True)
print("done", flush=True)
