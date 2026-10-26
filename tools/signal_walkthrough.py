# -*- coding: utf-8 -*-
"""信号中心真实库截图（R167）：收件箱/长消息全文/已发送/空态。

用法：python tools/signal_walkthrough.py [root] [输出后缀]
产出：output/real-signal<后缀>/inbox.png、detail.png、sent.png（空库为 empty.png）
"""
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    real_root = sys.argv[1] if len(sys.argv) > 1 else r"D:\agent资料\zcode项目记录"
    suffix = sys.argv[2] if len(sys.argv) > 2 else ""
    from PySide6.QtWidgets import QApplication
    import brain
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
    out = ROOT / "output" / f"real-signal{suffix}"
    try:
        w.resize(1280, 820)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        w.switchTo(w.signal_page)
        sp = w.signal_page
        settle(app, max_s=2.0)
        out.mkdir(parents=True, exist_ok=True)

        # 选收件箱条数最多的身份截图（不同 agent 收件量差异大）
        names = []
        try:
            names = [a["name"] for a in brain.list_agents(real_root)]
        except Exception:  # noqa: BLE001
            pass
        best, best_rows = (names[0] if names else "zcode"), []
        for n in names[:12]:
            sp.meCombo.setCurrentText(n)
            sp.reload()
            settle(app, max_s=1.0)
            if len(sp._rows) > len(best_rows):
                best, best_rows = n, sp._rows
        sp.meCombo.setCurrentText(best)
        sp.reload()
        settle(app, max_s=1.0)

        print(f"版面：窗口 {w.size().width()}x{w.size().height()}，页面 {sp.size().width()}x"
              f"{sp.size().height()}，计数标签 {sp.sumLabel.geometry().getRect()}，"
              f"预览视口 {sp.preview.viewport().width()}px", flush=True)
        if sp._rows:
            sp.grab().save(str(out / "inbox.png"))
            idx = max(range(len(sp._rows)),
                      key=lambda i: len(sp._rows[i].get("content") or ""))
            sp.listw.setCurrentRow(idx)
            settle(app, max_s=0.6)
            sp.grab().save(str(out / "detail.png"))
            full = sp._rows[idx].get("content") or ""
            tail = full.splitlines()[-1][-12:] if full else ""
            seen = sp.preview.toPlainText()
            print(f"身份 {best}：收件箱 {len(sp._rows)} 条 → inbox.png；"
                  f"最长 #{sp._rows[idx]['id']} 正文 {len(full)} 字 → detail.png；"
                  f"结尾「{tail}」可见={tail in seen}")
        else:
            sp.grab().save(str(out / "empty.png"))
            print(f"身份 {best}：收件箱为空 → empty.png（引导可见={sp.listw.count() == 0}）")

        sp.seg.setCurrentItem("sent")
        settle(app, max_s=1.0)
        sp.grab().save(str(out / "sent.png"))
        print(f"已发送 {len(sp._rows)} 条 → sent.png")
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
