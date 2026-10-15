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
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FACTORS = ("1.0", "1.25", "1.5")


def run_child(factor: str) -> tuple:
    env = dict(os.environ)
    env["QT_SCALE_FACTOR"] = factor
    env["QT_QPA_PLATFORM"] = "offscreen"
    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", factor],
                       cwd=str(ROOT), env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


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
    return 1 if bad else 0


def main() -> int:
    if "--child" in sys.argv:
        i = sys.argv.index("--child")
        return _check(sys.argv[i + 1] if len(sys.argv) > i + 1 else "1.0")

    print("=== 缩放回归：100% / 125% / 150% ===")
    rc = 0
    for f in FACTORS:
        code, out = run_child(f)
        print(out.strip() or "(无输出)")
        rc |= code
    print("\n结论：", "全部通过" if rc == 0 else "存在缩放问题（见上）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
