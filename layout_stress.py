# -*- coding: utf-8 -*-
"""布局压力测试（批次 3 复测）：920×600 逐页检测内容最小需求 vs 可用高度。"""
import os
import sys

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, r'D:\agent资料\zcode项目记录\AgentHub-多agent文件统一管理\AgentHub')

from PySide6.QtWidgets import QApplication
app = QApplication([])
import ui, tokens
tokens.apply_theme('dark')

w = ui.AgentHubWindow()
w.resize(920, 600)
w.show()

# 等扫描
import time
t0 = time.time()
while getattr(w.project_page, "snap", None) is None and time.time() - t0 < 20:
    app.processEvents()
    time.sleep(0.05)

pages = [
    ("总览", w.overview_page), ("项目", w.project_page), ("时间线", w.timeline_page),
    ("Agent 中心", w.hub_page), ("随记", w.notes_page), ("大脑", w.brain_page),
    ("流水·对账", w.ledger_page), ("统计", w.stats_page), ("运营报告", w.report_page),
    ("搜索", w.search_page), ("接入", w.connect_page),
    ("帮助", w.help_page), ("设置", w.settings_page),
]

risky = []
for name, page in pages:
    w.switchTo(page)
    app.processEvents()
    app.processEvents()
    h_avail = page.height()
    lay = page.layout()
    min_h = 0
    if lay is not None:
        min_h = lay.minimumSize().height()
    has_scroll = hasattr(page, "scroll")
    # 有 ScrollArea 的页面：内容超高时滚动，无压扁风险
    status = "滚动保护" if has_scroll else ("压扁风险" if min_h > h_avail else "OK")
    if status == "压扁风险":
        risky.append(name)
    print(f"  {name:<10} 可用 {h_avail:>4}px | 最小需求 {min_h:>4}px | {status}")

print()
if risky:
    print("压扁风险页:", risky)
else:
    print("压扁风险清零（无滚动保护的页面均不超限）")
sys.stdout.flush()
os._exit(0)
