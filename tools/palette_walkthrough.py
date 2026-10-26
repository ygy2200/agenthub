# -*- coding: utf-8 -*-
"""R174 交互走查：命令面板四态截图（空态精选 / 项目直达 / 工具检索 / 无结果）。

打印每态条目数与提示文案，并校验空态不再混入项目与 MCP 工具噪声。
"""
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))


def _labels(pal):
    return [pal.listw.item(i).text() for i in range(pal.listw.count())]


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    real_root = r"D:\agent资料\zcode项目记录"
    from PySide6.QtWidgets import QApplication
    import core
    import tokens
    import ui
    from helpers import pump, settle

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
    out = ROOT / "output" / "real-palette"
    try:
        w.resize(1280, 820)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        out.mkdir(parents=True, exist_ok=True)
        pal = w._palette
        cases = (
            ("empty", ""),
            ("project", "项目 打开"),
            ("tool", "hub_search"),
            ("none", "zzz不存在"),
        )
        for tag, q in cases:
            pal.popup()
            pal.search.setText(q)
            settle(app, max_s=2.0)
            labels = _labels(pal)
            pal.grab().save(str(out / f"palette_{tag}.png"))
            print(f"「{q or '(空)'}」→ {len(labels)} 项 · 提示：{pal.hint.text()}")
            print("   " + " | ".join(labels[:4]) + (" …" if len(labels) > 4 else ""))
            if tag == "empty":
                bad = [t for t in labels if t.startswith(("工具：", "项目："))]
                assert not bad, f"空态仍混入噪声条目：{bad[:3]}"
                assert labels and all(t.startswith(("打开：", "动作：", "外观："))
                                      for t in labels), labels
            if tag == "project":
                assert any(t.startswith("项目：") for t in labels), labels[:5]
            if tag == "tool":
                assert labels and all(t.startswith("工具：") for t in labels), labels[:5]
            if tag == "none":
                assert len(labels) == 2 and not pal.listw.item(0).flags(), labels
        pal.close()
        print("空态已排除项目/工具噪声，四态截图完成 →", out)
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
