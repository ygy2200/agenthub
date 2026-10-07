# -*- coding: utf-8 -*-
"""UI 改动冒烟：offscreen 实例化主窗，逐页切换 grab 存 PNG 供视觉验收。只读不改业务代码。"""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QPA_FONTDIR"] = r"C:\Windows\Fonts"  # offscreen 无自带字体，借用系统字体渲染中文
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # AgentHub 源码根（tools/ 上一级）

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont

import ui
from qfluentwidgets import setTheme, Theme

ROOT = r"D:\agent资料\zcode项目记录"
OUT = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "output", "ui_shots")

setTheme(Theme.LIGHT)
app = ui.QApplication(sys.argv)
app.setFont(QFont("Microsoft YaHei UI", 9))
w = ui.AgentHubWindow()
w.resize(1400, 900)  # 接近真实使用尺寸，避免内容被压扁误判
w.show()

if not w.root or not os.path.isdir(w.root):
    w.root = ROOT  # 冒烟固定指到真实大脑库，页面才有数据可渲染
    import brain
    brain.init_db(w.root)
    w.refresh()

PAGES = [("01-overview", w.overview_page), ("02-brain", w.brain_page),
         ("03-stats", w.stats_page), ("04-ledger", w.ledger_page),
         ("05-timeline", w.timeline_page), ("06-hub", w.hub_page)]
# 注意：journal/audit 是 ledger_page 的内部分段（不在导航栈），switchTo 只能用 ledger_page 本体
state = {"i": 0}


def snap():
    if state["i"] >= len(PAGES):
        QTimer.singleShot(100, app.quit)
        return
    name, page = PAGES[state["i"]]
    w.switchTo(page)

    def grab():
        pm = w.grab()
        ok = pm.save(os.path.join(OUT, name + ".png"))
        print(("saved " if ok else "FAILED ") + name, flush=True)
        state["i"] += 1
        QTimer.singleShot(150, snap)

    QTimer.singleShot(400, grab)


os.makedirs(OUT, exist_ok=True)
QTimer.singleShot(2500, snap)  # 先让后台建库/刷新跑完
rc = app.exec()
print("exec rc =", rc, flush=True)
os._exit(0)  # qfw item view 退出挂起兜底（已知坑）
