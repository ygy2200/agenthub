# -*- coding: utf-8 -*-
"""激活实验：用 AttachThreadInput 组合拳可靠激活 exe 窗口，看内容是否出现。
（不模拟鼠标点击，无副作用）"""
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
kernel32 = ctypes.windll.kernel32
user32.FindWindowW.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]

subprocess.run(["taskkill", "/F", "/T", "/IM", "AgentHub.exe"], capture_output=True)
time.sleep(0.8)
print("启动 exe ...", flush=True)
proc = subprocess.Popen([EXE])
hwnd = None
for i in range(40):
    hwnd = user32.FindWindowW(None, "AgentHub")
    if hwnd:
        break
    time.sleep(0.5)
print("hwnd:", hwnd, flush=True)
time.sleep(3.0)

H = ctypes.c_void_p(hwnd)

# ---- 激活前：截图 1 ----
from PIL import ImageGrab
ImageGrab.grab(all_screens=True).save(os.path.join(OUT, "act-1-before.png"))
print("before saved", flush=True)

# ---- AttachThreadInput 组合拳激活 ----
fg = user32.GetForegroundWindow()
fg_tid = user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), None)
cur_tid = kernel32.GetCurrentThreadId()
print("前台窗口 hwnd:", fg, "前台线程:", fg_tid, "本线程:", cur_tid, flush=True)
user32.AttachThreadInput(cur_tid, fg_tid, True)
user32.ShowWindow(H, 9)  # SW_RESTORE
user32.BringWindowToTop(H)
user32.SetForegroundWindow(H)
user32.SetFocus(H)
user32.AttachThreadInput(cur_tid, fg_tid, False)
time.sleep(0.5)
print("激活后前台窗口:", user32.GetForegroundWindow(), "( 目标:", hwnd, ")", flush=True)

# ---- 激活后：等 3 秒，截图 2 ----
time.sleep(3.0)
ImageGrab.grab(all_screens=True).save(os.path.join(OUT, "act-2-after.png"))
print("after saved", flush=True)

# ---- 再等 4 秒，截图 3（若有延迟绘制） ----
time.sleep(4.0)
ImageGrab.grab(all_screens=True).save(os.path.join(OUT, "act-3-after4s.png"))
print("after4s saved", flush=True)

subprocess.run(["taskkill", "/F", "/T", "/IM", "AgentHub.exe"], capture_output=True)
print("done", flush=True)
