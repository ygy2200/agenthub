# -*- coding: utf-8 -*-
"""缩放回归（P4）：在 100% / 125% / 150% 三个缩放档下逐页体检。

为什么单独做：本项目在 125% 屏上出过"字形被裁一半、拉大窗口才完整"的问题
（2026-10-09，根因是 Qt 对缩放因子取整 + DirectWrite 全 hinting，已用
PassThrough 策略 + PreferVerticalHinting 修复）。字号阶梯改过之后必须重跑，
否则"改了字号 = 重新踩坑"。

检查项（每个缩放档 × 18 页）：
  ① 页面能否渲染（grab 非空）
  ② 横向溢出：布局最小需求宽 > 可用宽（会被挤出可视区）
  ③ 纵向压扁：无滚动保护 且 最小需求高 > 可用高
  ④ 文本标签被压到 0 宽（缩放 + 省略策略下的"文字消失"）

用法：
    python tools/dpi_regression.py             # 跑三档（每档一个子进程）
    python tools/dpi_regression.py --child 1.25  # 只跑一档（内部用）
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FACTORS = ("1.0", "1.25", "1.5")


def _verdict(out: str) -> tuple:
    """从子进程 stdout 判定是否真的有缩放问题。

    2026-10-16 复盘：不能信退出码。离屏渲染下子进程打印完结论后会在退出路径上
    access violation（returncode 0xC0000005，实测 1.0/1.25 崩、1.5 不崩，纯看运气），
    父进程若 `rc |= returncode` 就会把"三档都 0 问题"误报成「存在缩放问题」。
    真正的判定依据是子进程打印的「问题 N 项」与「✗」明细行。
    """
    m = re.search(r"问题\s+(\d+)\s+项", out)
    if m:
        return int(m.group(1)), out
    return None, out          # 没打印结论（崩得更早）→ 交给出退出码处理


def run_child(factor: str, retries: int = 2) -> tuple:
    env = dict(os.environ)
    env["QT_SCALE_FACTOR"] = factor
    env["QT_QPA_PLATFORM"] = "offscreen"
    last = (None, "")
    for attempt in range(max(1, retries)):
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", factor],
                           cwd=str(ROOT), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        n, _ = _verdict(out)
        if n is not None:
            # 拿到了明确结论（哪怕进程随后崩了）——以结论为准
            return (1 if n else 0), out
        last = (r.returncode, out)
    # 始终没拿到结论：工具自身不稳，报一个可识别的专用码（-9），由上层提示重跑
    return -9, last[1]


def _check(factor: str) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT))
    from PySide6.QtWidgets import QApplication, QScrollArea, QLabel  # noqa: E402
    from PySide6.QtGui import QGuiApplication  # noqa: E402
    from PySide6.QtCore import Qt  # noqa: E402
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication([])
    import actions  # noqa: E402
    import tokens  # noqa: E402
    import ui  # noqa: E402
    tokens.apply_theme("dark")

    w = ui.AgentHubWindow()
    w.resize(1280, 820)
    w.show()
    import time  # noqa: E402
    t0 = time.time()
    while getattr(w.project_page, "snap", None) is None and time.time() - t0 < 25:
        app.processEvents()
        time.sleep(0.05)

    bad = []
    for _attr, title, _i, _g, _k, _c in actions.PAGES:
        page = actions.page_of(w, _attr)
        if page is None:
            bad.append(f"{title}: 缺页")
            continue
        w.switchTo(page)
        for _ in range(5):
            app.processEvents()
        pm = page.grab()
        if pm.isNull() or pm.width() < 50:
            bad.append(f"{title}: 渲染失败")
            continue
        lay = page.layout()
        if lay is None:
            continue
        ms = lay.minimumSize()
        avail_w, avail_h = page.width(), page.height()
        prot = bool(page.findChildren(QScrollArea))
        if ms.width() > avail_w + 2:
            bad.append(f"{title}: 横向溢出 需求{ms.width()} > 可用{avail_w}")
        if (not prot) and ms.height() > avail_h + 2:
            bad.append(f"{title}: 纵向压扁 需求{ms.height()} > 可用{avail_h}")
        # 文本被压到 0 宽（省略策略失效 → 文字整行消失）
        gone = [lb.text()[:16] for lb in page.findChildren(QLabel)
                if lb.text() and lb.isVisible() and lb.width() <= 1
                and not lb.property("keepWidth")]
        if len(gone) > 3:
            bad.append(f"{title}: {len(gone)} 个可见文本被压到 0 宽（例：{gone[:2]}）")

    print(f"[缩放 {factor}] 问题 {len(bad)} 项")
    for x in bad:
        print("   ✗", x)
    # 2026-10-16 复盘修复：子进程必须**硬退出**。原先 return 后走 Qt 正常析构，
    # 而离屏渲染下窗口/线程析构会间歇性 abort（exit 127，"QThreadStorage: entry
    # destroyed before end of thread"）。父进程把 127 当成"有缩放问题"，
    # 于是明明三档都 0 问题却报「存在缩放问题」——实测连跑三次 exit 0/127/0。
    # 本项目的既定解法见 window._hard_exit：flush 后 os._exit 跳过全部析构回调。
    sys.stdout.flush()
    sys.stderr.flush()
    ui._hard_exit(1 if bad else 0)


def main() -> int:
    if "--child" in sys.argv:
        i = sys.argv.index("--child")
        return _check(sys.argv[i + 1] if len(sys.argv) > i + 1 else "1.0")

    print("=== 缩放回归：100% / 125% / 150% ===")
    rc, unstable = 0, []
    for f in FACTORS:
        code, out = run_child(f)
        print(out.strip() or "(无输出)")
        if code == -9:
            unstable.append(f)
        else:
            rc |= code
    if unstable:
        print(f"\n⚠ 工具不稳定：{unstable} 档的子进程始终没打印结论（离屏 Qt 退出路径偶发"
              f" access violation）。请单独重跑：python tools/dpi_regression.py")
        return 2
    print("\n结论：", "全部通过" if rc == 0 else "存在缩放问题（见上）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
