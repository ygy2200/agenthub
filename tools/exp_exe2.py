# -*- coding: utf-8 -*-
"""实验 H2a：exe 窗口先移出屏幕外等 15 秒（让内容加载完），再移回截图。
用户可见时间最短（出现瞬间 + 最后约 2 秒）。"""
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
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_SHOWWINDOW = 0x0040

print("启动 exe ...", flush=True)
t0 = time.time()
proc = subprocess.Popen([EXE])
hwnd = None
for i in range(90):
    hwnd = user32.FindWindowW(None, "AgentHub")
    if hwnd:
        break
    time.sleep(0.5)
if not hwnd:
    print("FAIL: 未找到窗口", flush=True)
    proc.kill(); sys.exit(1)
print(f"窗口出现: 用时 {time.time()-t0:.1f}s hwnd={hwnd}", flush=True)

# 1) 立即移到屏幕外，避免打扰
user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_NOTOPMOST),
                    -3200, -3200, 1180, 780, 0)
print("已移出屏幕外，等待 15 秒让内容加载 ...", flush=True)
time.sleep(15)

# 1.5) 枚举 exe 进程加载的模块（找 d3dcompiler / Qt 相关）
import ctypes.wintypes as _wt
psapi = ctypes.windll.psapi
kernel32 = ctypes.windll.kernel32
hproc = kernel32.OpenProcess(0x0410, False, proc.pid)  # QUERY_INFO|VM_READ
if hproc:
    mods = (ctypes.c_void_p * 1024)()
    needed = _wt.DWORD()
    if psapi.EnumProcessModules(ctypes.c_void_p(hproc), ctypes.byref(mods), ctypes.sizeof(mods), ctypes.byref(needed)):
        cnt = needed.value // ctypes.sizeof(ctypes.c_void_p)
        names = []
        for i in range(cnt):
            buf = ctypes.create_unicode_buffer(512)
            psapi.GetModuleBaseNameW(ctypes.c_void_p(hproc), ctypes.c_void_p(mods[i]), buf, 512)
            names.append(buf.value)
        print(f"exe 进程已加载模块 {len(names)} 个；关键项：", flush=True)
        for kw in ("d3d", "dx", "Qt6", "opengl", "shader"):
            hit = [n for n in names if kw.lower() in n.lower()]
            print(f"  [{kw}]: {hit}", flush=True)
    kernel32.CloseHandle(ctypes.c_void_p(hproc))
else:
    print("OpenProcess 失败:", kernel32.GetLastError(), flush=True)

# 2) 移回屏幕内（右下角，置顶）
user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST),
                    1620, 680, 900, 720, SWP_SHOWWINDOW)
time.sleep(2.0)

# 3) 截图（连续两张，间隔 3 秒）
from PIL import ImageGrab
img = ImageGrab.grab(all_screens=True)
img.save(os.path.join(OUT, "exe2-full.png"))
print("saved exe2-full.png", flush=True)
time.sleep(3.0)
img2 = ImageGrab.grab(all_screens=True)
img2.save(os.path.join(OUT, "exe2-full-b.png"))
print("saved exe2-full-b.png", flush=True)

# 4) 硬杀
proc.kill()
print("done", flush=True)
