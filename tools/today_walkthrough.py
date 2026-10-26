# -*- coding: utf-8 -*-
"""R176 真实工作台走查：六段摘要/分组标题/行 tooltip/窄窗截图（只读）。"""
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))


def _prepare_demo():
    """六段演示库（只写 output/ 下的独立根，不碰真实大脑）：六类任务各一条。"""
    import datetime
    import brain
    root = ROOT / "output" / "real-today-demo-root"
    if not (root / "_hub" / "brain.db").is_file():
        root.mkdir(parents=True, exist_ok=True)
        brain.init_db(str(root))
        r = str(root)
        brain.task_add(r, "演示：可认领的 P1 任务", project="演示项目", priority=1,
                       due="2026-10-30")
        _, d = brain.task_add(r, "演示：正在做的任务", project="演示项目")
        brain.task_claim(r, d, agent="zcode")
        _, w1 = brain.task_add(r, "演示：前置任务", project="演示项目")
        brain.task_add(r, "演示：等待前置完成", project="演示项目", deps=str(w1))
        _, b = brain.task_add(r, "演示：失败后指数退避", project="演示项目")
        brain.task_retry(r, b, reason="演示：外部 API 超时，按退避重试", delay=900)
        _, f = brain.task_add(r, "演示：等外部条件再处理", project="演示项目")
        brain.task_defer(r, f, until=(datetime.date.today() + datetime.timedelta(days=2)).isoformat(),
                         reason="演示：等对方回复")
    return str(root)


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    demo = "demo" in sys.argv[1:]
    real_root = _prepare_demo() if demo else r"D:\agent资料\zcode项目记录"
    from PySide6.QtWidgets import QApplication
    import core
    import tokens
    import ui
    from helpers import settle

    app = QApplication.instance() or QApplication([])
    from PySide6.QtGui import QFontDatabase
    for f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"):
        if os.path.exists(f):
            QFontDatabase.addApplicationFont(f)
            break
    tokens.apply_theme("dark")
    orig = core.get_root
    core.get_root = lambda: real_root
    w = ui.AgentHubWindow()
    out = ROOT / "output" / ("real-today-demo" if demo else "real-today")
    try:
        w.resize(1280, 820)
        w.show()
        w.switchTo(w.today_page)
        settle(app, max_s=2.0)
        page = w.today_page
        page.reload()
        settle(app, max_s=1.0)
        out.mkdir(parents=True, exist_ok=True)
        print("摘要：" + page.sumLabel.text())
        print("日结：" + page.digestLine.text())
        heads = [page.listw.item(i).text() for i in range(page.listw.count())
                 if page.listw.item(i).text().startswith("──")]
        print("分组：" + " / ".join(heads))
        shown = 0
        for i in range(page.listw.count()):
            it = page.listw.item(i)
            if it.text().startswith("──") or shown >= 2:
                continue
            print("行：" + it.text())
            print("tip：" + it.toolTip().replace("\n", " / "))
            shown += 1
        page.grab().save(str(out / "today.png"))
        w.resize(920, 600)
        settle(app, max_s=1.0)
        page.grab().save(str(out / "today-narrow.png"))
        print(f"窄窗 {page.width()}x{page.height()}；截图 → {out}")
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
