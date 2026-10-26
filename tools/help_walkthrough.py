# -*- coding: utf-8 -*-
"""帮助页导航真实走查（R172）：目录、锚点跳转、搜索结果、无结果、窄窗。

用法：python tools/help_walkthrough.py [root] [输出后缀]
产出：output/real-help<后缀>/ 下 01-toc / 02-jump / 03-search / 04-no-result / 05-narrow。
"""
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))


def _shot(app, page, path, settle):
    settle(app, max_s=0.5)
    time.sleep(0.35)
    settle(app, max_s=0.3)
    page.grab().save(str(path))


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
    out = ROOT / "output" / f"real-help{suffix}"
    try:
        w.resize(1280, 820)
        w.show()
        w.switchTo(w.help_page)
        page = w.help_page
        assert pump(app, lambda: page.isVisible(), 20), "帮助页未显示"
        out.mkdir(parents=True, exist_ok=True)
        settle(app, max_s=1.0)

        print(f"[目录] {page.tocCount.text()}；共 {len(page.SECTIONS)} 节", flush=True)
        for i in range(page.tocList.count()):
            print(f"  {page.tocList.item(i).text()}", flush=True)
        _shot(app, page, out / "01-toc.png", settle)

        page.tocList.setCurrentRow(page.tocList.count() - 1)
        settle(app, max_s=0.6)
        print(f"[跳转] {page.atLabel.text()}；滚动值 "
              f"{page.browser.verticalScrollBar().value()}", flush=True)
        _shot(app, page, out / "02-jump.png", settle)

        sb = page.browser.verticalScrollBar()
        sb.setValue(sb.maximum())
        settle(app, max_s=0.6)
        print(f"[滚动联动] 目录第 {page.tocList.currentRow() + 1} 项；{page.atLabel.text()}；"
              f"回顶可用 {page.topBtn.isEnabled()}", flush=True)
        _shot(app, page, out / "02b-scroll-spy.png", settle)
        page.topBtn.click()
        settle(app, max_s=0.6)
        print(f"[回到顶部] 滚动值 {sb.value()}；目录第 {page.tocList.currentRow() + 1} 项；"
              f"回顶可用 {page.topBtn.isEnabled()}", flush=True)

        page.tocFilter.setText("接入")
        settle(app, max_s=0.6)
        print(f"[搜索 接入] {page.tocCount.text()}；高亮 {len(page.browser.extraSelections())} 处；"
              f"{page.atLabel.text()}", flush=True)
        _shot(app, page, out / "03-search.png", settle)

        page.tocFilter.setText("撤销")
        settle(app, max_s=0.6)
        print(f"[搜索 撤销] {page.tocCount.text()}；高亮 {len(page.browser.extraSelections())} 处；"
              f"{page.atLabel.text()}", flush=True)
        _shot(app, page, out / "03b-search-highlight.png", settle)

        page.tocFilter.setText("绝对不存在R172")
        settle(app, max_s=0.6)
        print(f"[无结果] {page.tocCount.text()}；{page.atLabel.text()}", flush=True)
        _shot(app, page, out / "04-no-result.png", settle)

        page.tocFilter.clear()
        w.resize(920, 600)
        settle(app, max_s=1.0)
        print(f"[窄窗 920x600] 目录 {page.tocList.width()}px / 正文 "
              f"{page.browser.width()}x{page.browser.height()}px", flush=True)
        _shot(app, page, out / "05-narrow.png", settle)
        print(f"截图目录：{out}", flush=True)
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
