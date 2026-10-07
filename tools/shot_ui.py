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

if os.environ.get("SHOT_DEMO"):  # 只为验收时间线形态：monkeypatch 内存演示数据，不落库
    import brain as _brain
    _demo = [
        {"id": 3, "ts": "2026-10-07T14:20:00", "source": "human", "content": "随记页时间线验收演示——行是线不是卡", "tags": "", "link": "", "mood": "灵感"},
        {"id": 2, "ts": "2026-10-07T10:02:00", "source": "human", "content": "第二条演示：心情徽章配色是否顺眼", "tags": "", "link": "", "mood": "笃定"},
        {"id": 1, "ts": "2026-10-06T22:31:00", "source": "human", "content": "昨天的演示条目——按日期分组", "tags": "", "link": "", "mood": "疑问"},
    ]
    _brain.list_notes = lambda root, limit=20: _demo[:max(0, min(int(limit), 200))]

PAGES = [("01-overview", w.overview_page), ("02-notes", w.notes_page),
         ("03-brain", w.brain_page), ("04-stats", w.stats_page),
         ("05-ledger", w.ledger_page), ("06-timeline", w.timeline_page),
         ("07-hub", w.hub_page)]
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
