# -*- coding: utf-8 -*-
"""实验 F：实测用户 exe 的 DPI 意识 + 截图取证。窗口预计显示约 4 秒后自动关闭。"""
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
shcore = ctypes.windll.shcore
kernel32 = ctypes.windll.kernel32

user32.FindWindowW.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]

print("启动 exe:", EXE, flush=True)
t0 = time.time()
proc = subprocess.Popen([EXE])

hwnd = None
for i in range(80):  # 最多等 40 秒
    hwnd = user32.FindWindowW(None, "AgentHub")
    if hwnd:
        break
    time.sleep(0.5)

if not hwnd:
    print("FAIL: 40 秒内未找到 AgentHub 窗口", flush=True)
    proc.terminate()
    sys.exit(1)

print(f"窗口出现，用时 {time.time()-t0:.1f}s，hwnd={hwnd}", flush=True)
time.sleep(1.5)  # 等渲染完成

# 1) 窗口物理矩形
rect = wt.RECT()
user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
print(f"窗口物理 rect: ({rect.left},{rect.top})-({rect.right},{rect.bottom}) "
      f"宽={rect.right-rect.left} 高={rect.bottom-rect.top}", flush=True)

# 2) DPI awareness
pid = wt.DWORD()
user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
print("PID:", pid.value, flush=True)
hproc = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
awareness = ctypes.c_int(-1)
hr = shcore.GetProcessDpiAwareness(ctypes.c_void_p(hproc), ctypes.byref(awareness))
print(f"GetProcessDpiAwareness hr={hr} value={awareness.value} "
      f"(0=UNAWARE 1=SYSTEM_AWARE 2=PER_MONITOR)", flush=True)

# 3) 全屏截图
from PIL import ImageGrab
img = ImageGrab.grab(all_screens=True)
img.save(os.path.join(OUT, "exe-full.png"))
print("saved exe-full.png", img.size, flush=True)

# 4) 关闭 exe
user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)  # WM_CLOSE
for i in range(10):
    if proc.poll() is not None:
        break
    time.sleep(0.5)
if proc.poll() is None:
    proc.kill()
print("exe 已关闭", flush=True)
