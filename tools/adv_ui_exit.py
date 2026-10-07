# -*- coding: utf-8 -*-
"""对抗性退出测试：随记功能上首页后的退出回归——四场景×5轮，抓偶发崩溃/traceback。

场景：
  A 写随记后 100ms 退出（InfoBar 显示中关窗）
  B 心情下拉 showPopup 展开态退出（qfw item view 退出挂起坑的攻击面）
  C 切遍全部导航页后退出
  D 启动后立即退出（show 后 0ms close）
offscreen 运行；退出码非 0 或 stderr 出现 Traceback 即判失败。
"""
import os
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from PySide6.QtCore import QTimer

import brain
import ui
from qfluentwidgets import setTheme, Theme

setTheme(Theme.LIGHT)
app = ui.QApplication(sys.argv)

# 测试写随记（场景A）——必须用临时库，绝不碰真实 brain.db（2026-10-07 污染教训）
TMP = tempfile.mkdtemp(prefix="agenthub-advexit-")
brain.init_db(TMP)

w = ui.AgentHubWindow()
w.root = TMP  # 覆盖配置里的真实根目录，页面 reload 全部走临时库
SCENES = ["A_write_exit", "B_popup_exit", "C_switch_all_exit", "D_instant_exit"]
ROUNDS = 5
state = {"i": 0}


def do_scene():
    if state["i"] >= len(SCENES) * ROUNDS:
        print("ADV-EXIT ALL PASS", flush=True)
        QTimer.singleShot(50, app.quit)
        return
    scene = SCENES[state["i"] % len(SCENES)]
    w.show()

    if scene == "A_write_exit":
        w.switchTo(w.overview_page)
        w.overview_page.noteInput.setText(f"对抗退出测试 {state['i']}")
        w.overview_page.save_note()  # InfoBar 显示中即关窗
    elif scene == "B_popup_exit":
        w.switchTo(w.overview_page)
        w.overview_page.noteMood.showPopup()  # 展开态关窗
    elif scene == "C_switch_all_exit":
        for p in (w.overview_page, w.project_page, w.timeline_page, w.hub_page,
                  w.brain_page, w.ledger_page, w.stats_page, w.report_page,
                  w.search_page):
            w.switchTo(p)

    QTimer.singleShot(150 if scene != "D_instant_exit" else 0, w.close)
    QTimer.singleShot(900, lambda: (print(f"scene {scene} #{state['i']} ok", flush=True),
                                    state.__setitem__("i", state["i"] + 1),
                                    do_scene()))


QTimer.singleShot(1500, do_scene)
rc = app.exec()
print("exec rc =", rc, flush=True)
ui._hard_exit(rc if rc else 0)  # qfw item view 退出挂起兜底（已知坑）
