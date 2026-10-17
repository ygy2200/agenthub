# -*- coding: utf-8 -*-
"""布局压力测试（Round 1 / 2026-10-13 扩覆盖）：逐页检测内容最小需求 vs 可用尺寸。

为什么改：
① 原脚本硬编码 13 页，漏了 actions.PAGES 里的 5 页（工作台 / 信号中心 / 治理中心 /
   编排中心 / 组件预览）——新增的页面没人守压扁风险；
② 滚动保护检测用 `hasattr(page, "scroll")`，换个属性名就误判成「无保护」。
现在：页面清单直接取 actions.PAGES（与左侧导航 / 命令面板同源，杜绝两份清单漂移），
滚动保护用 findChildren(QScrollArea) 递归识别（qfw ScrollArea 是 QScrollArea 子类）。
"""
import os
import sys
import tempfile

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

# 2026-10-17 R5：间歇性退出段错误取证（3 连跑 1 次 exit=139，输出已完整=退出竞态）。
# 与 smoke_ui.py 同一病原；faulthandler 落盘崩溃点，修完再摘。
import faulthandler
faulthandler.enable(open(os.path.join(tempfile.gettempdir(), "AgentHub-stress-fault.log"),
                        "a", encoding="utf-8"))
sys.path.insert(0, str(os.path.dirname(os.path.abspath(__file__))))  # 回归器子进程 cwd 无关

from PySide6.QtWidgets import QApplication, QScrollArea
app = QApplication([])
# 2026-10-16：必须按**生产字体口径**测量。此前没调 _setup_app_font，离屏下微软雅黑
# 加载失败回退默认窄体，量到的 minimumSize 与真实不符——「流水·对账 920×600 压扁」
# 从第一轮报到现在，就是默认字体下某个标签 sizeHint 584px 造成的假象
# （生产字体口径实测仅 394px，根本不溢出）。
import window as _winmod
_winmod._setup_app_font(app)
import ui, tokens, actions
tokens.apply_theme('dark')

w = ui.AgentHubWindow()
w.show()

# 等扫描
import time
t0 = time.time()
while getattr(w.project_page, "snap", None) is None and time.time() - t0 < 20:
    app.processEvents()
    time.sleep(0.05)

SIZES = [(920, 600), (1280, 800)]   # 最小窗 + 常见窗


def _scroll_protected(page):
    return bool(page.findChildren(QScrollArea))


def _min_size(page):
    lay = page.layout()
    if lay is None:
        return 0, 0
    ms = lay.minimumSize()
    return ms.height(), ms.width()


risky = []
for wpx, hpx in SIZES:
    w.resize(wpx, hpx)
    app.processEvents()
    app.processEvents()
    print(f"\n== 窗口 {wpx}×{hpx} ==")
    for attr, title, _icon, _group, _kw, _ctrl in actions.PAGES:
        page = actions.page_of(w, attr)
        if page is None:
            print(f"  {title:<10} 缺页（未实例化）")
            risky.append((f"{wpx}x{hpx}", title, "缺页"))
            continue
        w.switchTo(page)
        app.processEvents()
        app.processEvents()
        min_h, min_w = _min_size(page)
        h_avail, w_avail = page.height(), page.width()
        prot = _scroll_protected(page)
        v_risk = (not prot) and min_h > h_avail
        h_risk = min_w > w_avail
        flags = []
        if v_risk:
            flags.append("压扁风险")
        if h_risk:
            flags.append("横向溢出")
        if flags:
            risky.append((f"{wpx}x{hpx}", title, "/".join(flags)))
        flag = "/".join(flags) if flags else "OK"
        print(f"  {title:<10} 可用 {h_avail:>4}×{w_avail:<4} | 最小需求 {min_h:>4}×{min_w:<4} | "
              f"{'滚动保护' if prot else '无滚动  '} | {flag}")

print()
if risky:
    print(f"风险 {len(risky)} 处：")
    for s in risky:
        print("   ", s)
else:
    print("压扁 / 横向溢出风险清零")
sys.stdout.flush()
# R5（2026-10-17）：退出走 window._hard_exit（TerminateProcess），不用裸 os._exit。
# 病因：os._exit 的 Windows 进程终止仍会触发线程终止+Qt TLS 清理，与半死线程竞态——
# 间歇性 access violation（faulthandler 实证：本脚本 8 连跑 2 中；smoke 6 跑 3 中同病原）。
# window._hard_exit 跳过全部 DLL 卸载回调，实测 10+ 次无段错误。曾试过先收 worker 再
# os._exit——无效（崩在 ExitProcess 的 TLS 清理，不在 worker），已回退该写法。
_winmod._hard_exit(0)
