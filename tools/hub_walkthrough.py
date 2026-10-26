# -*- coding: utf-8 -*-
"""Agent 中心真实库截图（R169）：卡片状态行宽度与裁切核验。

用法：python tools/hub_walkthrough.py [root] [输出后缀]
产出：output/real-hub<后缀>/cards.png、cards-narrow.png
"""
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))


def _status_labels(hp):
    from uikit import ElidedLabel
    out = []
    for i in range(hp.cardsRow.count()):
        card = hp.cardsRow.itemAt(i).widget()
        if card is None:
            continue
        name = card.layout().itemAt(0).widget().text()
        for lb in card.findChildren(ElidedLabel):
            if lb.fullText().startswith(("●", "○")):
                out.append((name, lb))
    return out


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
    out = ROOT / "output" / f"real-hub{suffix}"
    try:
        w.resize(1280, 820)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        w.switchTo(w.hub_page)
        hp = w.hub_page
        assert pump(app, lambda: hasattr(hp, "_reg") and bool(hp.agents), 30), "探测未完成"
        settle(app, max_s=2.0)
        out.mkdir(parents=True, exist_ok=True)
        for name, lb in _status_labels(hp):
            print(f"{name}: 「{lb.text()}」 宽 {lb.width()}/{lb.sizeHint().width()}"
                  f" 全量={lb.text() == lb.fullText()} tooltip={lb.toolTip()}", flush=True)
        hp.grab().save(str(out / "cards.png"))
        print(f"1280 窗：{hp.cardsRow.count()} 张卡 → cards.png", flush=True)

        w.resize(1000, 820)
        settle(app, max_s=1.5)
        clipped = [(name, lb.text(), lb.fullText()) for name, lb in _status_labels(hp)
                   if lb.text() != lb.fullText()]
        hp.grab().save(str(out / "cards-narrow.png"))
        print(f"1000 窗：状态行仍被省略 {len(clipped)} 条"
              f"{'（' + str(clipped) + '）' if clipped else ''} → cards-narrow.png", flush=True)
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
