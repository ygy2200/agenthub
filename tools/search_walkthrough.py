# -*- coding: utf-8 -*-
"""搜索页真实查询态截图（R137 建，R166 补空态/无结果态）。

用法：python tools/search_walkthrough.py [root] [输出后缀]
产出：output/real-search<后缀>/empty.png、no_result.png、hit_<n>.png
"""
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))

QUERIES = ["署名闸 冒名", "热力图 间距", "备份 恢复", "recency 排序"]


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    real_root = sys.argv[1] if len(sys.argv) > 1 else r"D:\agent资料\zcode项目记录"
    suffix = sys.argv[2] if len(sys.argv) > 2 else ""
    from PySide6.QtWidgets import QApplication, QPushButton
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
    out = ROOT / "output" / f"real-search{suffix}"
    try:
        w.resize(1280, 820)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        w.switchTo(w.search_page)
        sp = w.search_page
        settle(app, max_s=2.0)
        out.mkdir(parents=True, exist_ok=True)

        sp.grab().save(str(out / "empty.png"))
        hint = getattr(sp.result, "_empty_hint_widget", None)
        cta = [b for b in hint.findChildren(QPushButton) if b.text() == "搜索最近项目"] if hint else []
        print(f"空态：引导 {bool(hint and hint.isVisible())}，CTA {len(cta)} 个 → empty.png")

        miss = "zzqx166qqzz"  # 纯拉丁乱串：真实库里不会命中中文记录
        sp.edit.setText(miss)
        sp.run()
        settle(app, max_s=6.0)
        sp.grab().save(str(out / "no_result.png"))
        print(f"无结果「{miss}」→ no_result.png，预览：{sp.preview.toPlainText()[:60]!r}")

        for i, q in enumerate(QUERIES, 1):
            sp.edit.setText(q)
            sp.run()
            settle(app, max_s=6.0)
            sp.grab().save(str(out / f"hit_{i}.png"))
            row = sp.result.currentRow()
            kinds = [sp.view_hits[row][0]] if 0 <= row < len(sp.view_hits) else []
            print(f"查询「{q}」→ hit_{i}.png（结果行 {sp.result.count()}，选中 {kinds}）")
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
