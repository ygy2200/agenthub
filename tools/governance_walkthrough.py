# -*- coding: utf-8 -*-
"""治理中心真实库走查（R171）：八段主文案是否人话、空态/列表是否可读。

用法：python tools/governance_walkthrough.py [root] [输出后缀]
产出：output/real-governance<后缀>/ 下 approvals/handoffs/... 八张截图。
"""
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))

SEGMENTS = ("approvals", "handoffs", "quarantine", "consolidate",
            "decision", "view", "skill", "security")


def _shot(app, page, path, settle):
    settle(app, max_s=0.5)
    time.sleep(0.35)
    settle(app, max_s=0.3)
    page.grab().save(str(path))


def _empty_text(sub):
    from PySide6.QtWidgets import QLabel
    hint = getattr(sub.listw, "_empty_hint_widget", None)
    if hint is not None and hint.isVisible():
        return " / ".join(lb.text() for lb in hint.findChildren(QLabel))
    return ""


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    real_root = sys.argv[1] if len(sys.argv) > 1 else r"D:\agent资料\zcode项目记录"
    suffix = sys.argv[2] if len(sys.argv) > 2 else ""
    from PySide6.QtWidgets import QApplication
    import core
    import tokens
    import ui
    from helpers import pump, settle

    app = QApplication.instance() or QApplication([])
    for f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"):
        if os.path.exists(f):
            from PySide6.QtGui import QFontDatabase
            QFontDatabase.addApplicationFont(f)
            break
    tokens.apply_theme("dark")
    orig = core.get_root
    core.get_root = lambda: real_root
    w = ui.AgentHubWindow()
    out = ROOT / "output" / f"real-governance{suffix}"
    try:
        w.resize(1280, 820)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        w.switchTo(w.governance_page)
        page = w.governance_page
        assert pump(app, lambda: page.isVisible(), 20), "治理中心未显示"
        out.mkdir(parents=True, exist_ok=True)

        for key, sub in zip(SEGMENTS, page.subs):
            page.seg.setCurrentItem(key)
            page.reload()
            settle(app, max_s=1.0)
            print(f"[{sub.TITLE}] 摘要：{sub.sumLabel.text()}", flush=True)
            empty = _empty_text(sub)
            if empty:
                print(f"  空态：{empty}", flush=True)
            for i in range(min(sub.listw.count(), 6)):
                print(f"  {sub.listw.item(i).text()}", flush=True)
            if sub.listw.count() > 6:
                print(f"  ……共 {sub.listw.count()} 行", flush=True)
            _shot(app, page, out / f"{key}.png", settle)

        w.root = None
        page.reload()
        settle(app, max_s=0.6)
        print("[无根目录] 各段摘要：", flush=True)
        for key, sub in zip(SEGMENTS, page.subs):
            page.seg.setCurrentItem(key)
            print(f"  {sub.TITLE}：{sub.sumLabel.text()}", flush=True)
        _shot(app, page, out / "no-root.png", settle)
        print(f"截图目录：{out}", flush=True)
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
