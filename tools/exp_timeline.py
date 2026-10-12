# -*- coding: utf-8 -*-
"""时序实验 v2：自然启动 exe，连续采帧 11 秒（窗口自然出现、自然渲染），
每帧立刻裁剪窗口区+缩小存盘。结束后 taskkill /T 清理全部 AgentHub 进程。"""
import ctypes
import ctypes.wintypes as wt
import os
import statistics
import subprocess
import sys
import time

EXE = r"C:\Users\y\Desktop\AgentHub.exe"
OUT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "frames"))
os.makedirs(OUT, exist_ok=True)

user32 = ctypes.windll.user32
user32.FindWindowW.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]

# 先清掉任何历史残留
subprocess.run(["taskkill", "/F", "/T", "/IM", "AgentHub.exe"], capture_output=True)
time.sleep(1.0)

print("启动 exe ...", flush=True)
proc = subprocess.Popen([EXE])

from PIL import ImageGrab

t0 = time.time()
win_appear_t = None
stats = []
i = 0
while time.time() - t0 < 11:
    t = time.time() - t0
    if win_appear_t is None:
        hwnd = user32.FindWindowW(None, "AgentHub")
        if hwnd:
            win_appear_t = t
            print(f"窗口首次出现: t={t:.2f}s", flush=True)
    img = ImageGrab.grab(all_screens=True)
    crop = img.crop((600, 160, 1880, 1040))
    crop = crop.resize((crop.width // 2, crop.height // 2))
    crop.save(os.path.join(OUT, f"f{i:02d}-{t:05.2f}.png"))
    g = crop.convert("L")
    pix = list(g.getdata())
    stats.append((t, sum(pix) / len(pix), statistics.pvariance(pix)))
    i += 1
    time.sleep(0.35)

print(f"采帧完成：{i} 帧，窗口出现时刻: {win_appear_t}", flush=True)
print("时间(秒)  平均亮度  亮度方差  高方差=可能有内容", flush=True)
for t, avg, var in stats:
    mark = "*** CONTENT?" if var > 1800 else ""
    print(f"{t:6.2f}   {avg:7.1f}  {var:8.1f}  {mark}", flush=True)

# 硬杀整棵进程树
subprocess.run(["taskkill", "/F", "/T", "/IM", "AgentHub.exe"], capture_output=True)
print("已清理全部 AgentHub 进程", flush=True)
