# -*- coding: utf-8 -*-
"""R160 加速老化：全页 reload × N 轮，监控 RSS 内存曲线（泄漏=持续线性上涨）。"""
import ctypes
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))


def rss_mb() -> float:
    """当前进程工作集（MB）。"""
    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    # Win7+：kernel32 导出 K32GetProcessMemoryInfo；GetCurrentProcess 伪句柄是
    # -1，ctypes 默认 int restype 会截断成 1（err=6 INVALID_HANDLE，实测）——必须声明
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.K32GetProcessMemoryInfo.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)
    h = k32.GetCurrentProcess()
    if not k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
        return 0.0
    return pmc.WorkingSetSize / 1024 / 1024


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    from PySide6.QtWidgets import QApplication
    import actions
    import core
    import tokens
    import ui
    from helpers import pump

    app = QApplication.instance() or QApplication([])
    from PySide6.QtGui import QFontDatabase
    for f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"):
        if os.path.exists(f):
            QFontDatabase.addApplicationFont(f)
            break
    tokens.apply_theme("dark")
    core.get_root = lambda: r"D:\agent资料\zcode项目记录"
    w = ui.AgentHubWindow()
    w.show()
    pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)

    only = sys.argv[2] if len(sys.argv) > 2 else ""
    pages = [(a, getattr(w, a)) for a, *_ in actions.PAGES
             if getattr(w, a, None) and (not only or a == only)]
    base = rss_mb()
    samples = []
    for i in range(1, rounds + 1):
        for attr, page in pages:
            w.switchTo(page)
            if hasattr(page, "reload"):
                try:
                    page.reload()
                except Exception as e:  # noqa: BLE001
                    print(f"第 {i} 轮 {attr} reload 异常: {e}")
        if i % 10 == 0:
            app.processEvents()
            m = rss_mb()
            samples.append((i, m))
            print(f"第 {i:3d} 轮: RSS {m:.0f} MB（基线 {base:.0f}，涨 {m - base:+.0f}）")
    w.close()
    # 泄漏判定：最后 3 个采样点相对第 10 轮的涨幅（首几轮有缓存爬坡属正常）
    if len(samples) >= 2:
        early = samples[0][1]
        late = sum(s[1] for s in samples[-3:]) / 3
        growth = late - early
        verdict = ("疑似泄漏（持续上涨）" if growth > 80 else
                   "温和增长（缓存爬坡）" if growth > 30 else "稳定")
        print(f"\n老化结论：第 10 轮 {early:.0f} MB → 末段均值 {late:.0f} MB，"
              f"涨 {growth:+.0f} MB —— {verdict}")


if __name__ == "__main__":
    main()
