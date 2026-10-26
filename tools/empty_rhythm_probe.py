# -*- coding: utf-8 -*-
"""空态纵向节奏探针（R194）：量空态内部各元素的实际纵向间隔。

背景：`uikit.empty_state` 放进可伸缩容器时，Qt 把富余高度平摊给图标/标题/说明/按钮，
文字被拉成互不相干的四大块（920×600 时间线实测标题→说明 119px）。肉眼从截图只能
看出「怪」，量不出「差多少」，所以留一支可复跑的尺子。

用法：
  python tools/empty_rhythm_probe.py [root] [--size 920x600] [--page timeline_page]
      [--title 还没有工作记录] [--limit 12]

输出：每个元素的名字/纵向位置/高度，相邻间隔，以及超过阈值的间隔清单。
退出码：1 = 存在 > 阈值的间隔（可作门禁）；0 = 节奏正常。
"""
import os
import sys
from pathlib import Path

import _console  # noqa: E402,F401  控制台 UTF-8 守卫

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))

MAX_GAP = 12  # 设计值：SPACE_XS；超过这个数就是被拉伸分摊了


def _split(argv):
    opts, pos, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                k, v = a.split("=", 1)
                opts[k] = v
            elif i + 1 < len(argv):
                opts[a] = argv[i + 1]
                i += 1
            else:
                opts[a] = ""
        else:
            pos.append(a)
        i += 1
    return pos, opts


POS, OPTS = _split(sys.argv[1:])


def describe(w):
    from PySide6.QtWidgets import QLabel, QPushButton

    if isinstance(w, QLabel):
        return f"label:{w.text()[:24]}"
    if isinstance(w, QPushButton):
        return f"button:{w.text()[:16]}"
    return type(w).__name__


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    root = POS[0] if POS else str(ROOT / "_pytest_tmp" / "empty-root-r192")
    size = OPTS.get("--size", "920x600")
    page_attr = OPTS.get("--page", "timeline_page")
    title = OPTS.get("--title", "还没有工作记录")
    limit = int(OPTS.get("--limit", "12"))
    win_w, win_h = (int(x) for x in size.lower().split("x"))

    from PySide6.QtWidgets import QApplication, QLabel, QWidget

    import core
    import tokens
    import ui
    from helpers import pump, settle

    app = QApplication.instance() or QApplication([])
    tokens.apply_theme("dark")
    orig = core.get_root
    core.get_root = lambda: root
    w = ui.AgentHubWindow()
    try:
        w.resize(win_w, win_h)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        page = getattr(w, page_attr)
        w.switchTo(page)
        settle(app, max_s=3.0)
        hits = [lb for lb in page.findChildren(QLabel) if lb.isVisible()
                and (lb.text() or "").strip() == title]
        if not hits:
            print(f"✗ 在 {page_attr} 上没找到空态标题「{title}」（页面可能不是空态）")
            return 2
        target = hits[0]
        # 空态根容器 = 标题往上第一个直接持有多个子控件的祖先
        box = target.parentWidget()
        kids = [c for c in box.children() if isinstance(c, QWidget)]
        rows = []
        for c in kids:
            if not c.isVisible():
                continue
            g = c.geometry()
            rows.append((g.y(), g.height(), describe(c), c))
        rows.sort(key=lambda r: r[0])
        print(f"{page_attr} @ {win_w}x{win_h} · 空态容器 {box.width()}x{box.height()}")
        worst = []
        prev_bottom = None
        for y, h, name, _w in rows[:limit]:
            gap = "" if prev_bottom is None else f"  间隔 {y - prev_bottom}px"
            print(f"  y={y:4d} h={h:3d}  {name}{gap}")
            if prev_bottom is not None and y - prev_bottom > MAX_GAP:
                worst.append((y - prev_bottom, name))
            prev_bottom = y + h
        if worst:
            print(f"✗ {len(worst)} 处间隔超过 {MAX_GAP}px：" +
                  "、".join(f"{n}({g}px)" for g, n in worst))
            return 1
        print(f"✓ 所有间隔 ≤ {MAX_GAP}px")
        return 0
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    sys.exit(main())
