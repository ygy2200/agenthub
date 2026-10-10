# -*- coding: utf-8 -*-
"""诊断入口（不改动 ui.py）：复制 run_gui 的启动序列 + 全程写日志到文件。
用于「打包后窗口透明/不渲染」问题的进程内取证。"""
import os
import sys
import threading
import time
import traceback

LOG = r"D:\agent资料\zcode项目记录\AgentHub-多agent文件统一管理\AgentHub\output\diag-run.txt"
_log = open(LOG, "w", buffering=1, encoding="utf-8")


def L(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, file=_log, flush=True)


def _thook(args):
    L("!! 线程未捕获异常:", args.exc_type, args.exc_value)
    L("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))


def _hook(t, v, tb):
    L("!! 主线程未捕获异常:", t, v)
    L("".join(traceback.format_exception(t, v, tb)))


sys.excepthook = _hook
threading.excepthook = _thook

L("=== diag 启动 ===")
L("executable =", sys.executable)
L("frozen =", getattr(sys, "frozen", False))
L("_MEIPASS =", getattr(sys, "_MEIPASS", None))
L("cwd =", os.getcwd())
L("argv =", sys.argv)


def watchdog():
    time.sleep(7)
    frames = sys._current_frames()
    names = {t.ident: t.name for t in threading.enumerate()}
    L(f"--- watchdog: {len(frames)} 个线程 ---")
    for tid, fr in frames.items():
        stack = []
        depth = 0
        while fr and depth < 30:
            stack.append(f"{os.path.basename(fr.f_code.co_filename)}:{fr.f_lineno} in {fr.f_code.co_name}")
            fr = fr.f_back
            depth += 1
        L(f"[线程 {tid} {names.get(tid, '?')}]")
        for s in stack:
            L("    ", s)
    # 第二轮（更晚，看状态变化）
    time.sleep(6)
    L("--- watchdog 第二轮 ---")
    frames = sys._current_frames()
    for tid, fr in frames.items():
        stack = []
        depth = 0
        while fr and depth < 25:
            stack.append(f"{os.path.basename(fr.f_code.co_filename)}:{fr.f_lineno} in {fr.f_code.co_name}")
            fr = fr.f_back
            depth += 1
        L(f"[线程 {tid} {names.get(tid, '?')}]")
        for s in stack:
            L("    ", s)


threading.Thread(target=watchdog, daemon=True).start()

try:
    L("import ui ...")
    import ui

    L("ui 导入完成")
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import setTheme, Theme

    L("qt/qfw 导入完成")
    ui._install_crash_hook()
    L("crash hook 装好")
    setTheme(Theme.AUTO)
    L("setTheme(AUTO) 完成")
    app = QApplication(sys.argv)
    L("QApplication 建好; platform =", app.platformName())
    L("screens =", [(s.name(), s.geometry().getRect(), s.devicePixelRatio()) for s in app.screens()])
    app.setFont(QFont("Microsoft YaHei UI", 9))
    L("字体设置完成")
    w = ui.AgentHubWindow()
    L("窗口建好:", w.width(), "x", w.height(), " geo:", w.geometry().getRect())
    if os.environ.get("AGHUB_DIAG_NOMICA") == "1":
        try:
            w.setMicaEffectEnabled(False)
            L("【实验】已 setMicaEffectEnabled(False)")
        except Exception as e:
            L("setMicaEffectEnabled 失败:", e)
    w.show()
    L("窗口 show 完成; visible =", w.isVisible(), " isActive =", w.isActiveWindow())
    w.switchTo(w.connect_page)
    L("已切到接入页")

    def dump():
        try:
            L("== 状态 dump ==")
            L("visible =", w.isVisible(), " active =", w.isActiveWindow(), " opacity =", w.windowOpacity())
            L("size =", w.size().toTuple(), " pos =", w.pos().toTuple())
            L("windowState =", str(w.windowState()))
            L("devicePixelRatio =", w.devicePixelRatioF())
            L("updatesEnabled =", w.updatesEnabled())
            kids = w.findChildren(object)
            L("子孙控件数 =", len(kids))
            cp = w.connect_page
            try:
                L("接入页 rows =", cp.rows.count())
                L("接入页 pyLabel =", cp.pyLabel.text()[:60])
            except Exception as e:
                L("接入页状态读取失败:", e)
            L("central widget =", w.centralWidget().metaObject().className() if w.centralWidget() else None)
        except Exception as e:
            L("dump 异常:", e)
            L(traceback.format_exc())

    QTimer.singleShot(5000, dump)
    QTimer.singleShot(11000, dump)
    L("进入事件循环 ...")
    rc = app.exec()
    L("事件循环退出 rc =", rc)
except Exception as e:
    L("!! 顶层异常:", e)
    L(traceback.format_exc())
finally:
    L("=== diag 结束 ===")
