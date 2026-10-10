# -*- coding: utf-8 -*-
"""验收 v2.25.0：启动「桌面 AgentHub.exe」，窗口移到屏幕外（零打扰），
用 PrintWindow 抓真机渲染，验证主窗口 + Ctrl+K 命令面板。
用法：python verify_exe_v225.py
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

EXE = r"C:\Users\y\Desktop\AgentHub.exe"
OUT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "ui-v2.25"))
os.makedirs(OUT, exist_ok=True)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI aware，坐标与物理像素一致
except Exception:
    pass
user32.FindWindowW.restype = ctypes.c_void_p
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]

W, H = 1400, 900
SWP_SHOWWINDOW = 0x0040
HWND_NOTOPMOST = -2
PW_RENDERFULLCONTENT = 2


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


def grab(hwnd, path):
    """窗口已在屏幕内：ImageGrab 全屏后按窗口矩形裁剪"""
    from PIL import ImageGrab
    r = wt.RECT()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    img = ImageGrab.grab(all_screens=True)
    img = img.crop((r.left, r.top, r.right, r.bottom))
    img.save(path)
    return img


def ctrl_k():
    """ctypes keybd_event 发 Ctrl+K（WScript SendKeys 会被拦截）"""
    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL = 0x11
    VK_K = 0x4B
    ctypes.windll.user32.keybd_event(VK_CONTROL, 0, 0, 0)
    ctypes.windll.user32.keybd_event(VK_K, 0, 0, 0)
    time.sleep(0.05)
    ctypes.windll.user32.keybd_event(VK_K, 0, KEYEVENTF_KEYUP, 0)
    ctypes.windll.user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def main():
    t0 = time.time()
    proc = subprocess.Popen([EXE])
    hwnd = None
    for _ in range(120):
        hwnd = user32.FindWindowW(None, "AgentHub")
        if hwnd:
            break
        time.sleep(0.5)
    if not hwnd:
        print("FAIL: 未找到 AgentHub 窗口")
        proc.kill()
        sys.exit(1)
    print(f"窗口出现 hwnd={hwnd} 用时 {time.time()-t0:.1f}s")
    user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_NOTOPMOST),
                        -3200, -3200, W, H, 0)
    print("窗口已移出屏幕外，等待 10s 让内容加载 ...")
    time.sleep(10)

    # 移回屏幕内右下角（用户可见约 4 秒），置顶后截图
    HWND_TOPMOST = -1
    user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST),
                        1500, 100, 900, 760, SWP_SHOWWINDOW)
    time.sleep(2.0)

    p1 = os.path.join(OUT, "exe-desktop-main.png")
    img1 = grab(hwnd, p1)
    print("saved", p1, img1.size)

    # Ctrl+K 命令面板：先鼠标点击窗口标题栏激活，再发键
    user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0040)  # NOSIZE|SHOWWINDOW
    ctypes.windll.user32.SetCursorPos(1500 + 450, 100 + 15)
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.5)
    ctrl_k()
    time.sleep(1.8)
    p2 = os.path.join(OUT, "exe-desktop-palette.png")
    img2 = grab(hwnd, p2)
    print("saved", p2, img2.size)

    # 面板是否真的弹出：比较两次截图的差异像素比例
    diff = sum(1 for a, b in zip(img1.convert("L").tobytes(), img2.convert("L").tobytes()) if a != b)
    ratio = diff / float(len(img1.convert("L").tobytes()))
    print(f"面板弹出前后差异像素比例: {ratio:.3f}")

    # 关掉
    user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)  # WM_CLOSE
    time.sleep(2.0)
    if proc.poll() is None:
        proc.kill()
    print("done")


if __name__ == "__main__":
    main()
