# -*- coding: utf-8 -*-
"""真实库走查：连真实 root 启动窗口逐页截图（只读，不种子不改库）。
用法：python tools/real_walkthrough.py [root] [dark|light] [输出后缀] [--size 920x600] [--pages stats,notes]
默认 1280×820 全页；--size 可指定最小窗口 920x600 复核首屏。"""
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))

def _split_args(argv: list) -> tuple:
    """位置参数（root/主题/后缀）与 `--k v` / `--k=v` 选项分离；选项值不混进位置参数。"""
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


POS, OPTS = _split_args(sys.argv[1:])
THEME = POS[1] if len(POS) > 1 else "dark"
SUFFIX = POS[2] if len(POS) > 2 else ""
OUT = ROOT / "output" / f"real-shots{SUFFIX}"


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    real_root = POS[0] if POS else r"D:\agent资料\zcode项目记录"
    size = OPTS.get("--size", "1280x820")
    want = {s.strip() for s in OPTS.get("--pages", "").split(",") if s.strip()}
    try:
        win_w, win_h = (int(x) for x in size.lower().split("x"))
    except ValueError:
        print(f"错误：--size 需为 宽x高（收到 {size}）")
        return 2
    from PySide6.QtWidgets import QApplication
    import actions
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
    tokens.apply_theme(THEME)
    orig = core.get_root
    core.get_root = lambda: real_root
    w = ui.AgentHubWindow()
    try:
        w.resize(win_w, win_h)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        pump(app, lambda: bool(getattr(w.hub_page, "agents", None)), 8.0)
        OUT.mkdir(parents=True, exist_ok=True)
        for attr, title, *_ in actions.PAGES:
            if want and attr not in want:
                continue
            page = getattr(w, attr, None)
            if page is None:
                continue
            w.switchTo(page)
            # 统计/大脑页是异步取数：不等它落地就截图，拿到的是一屏骨架，
            # 走查会误判「页面空白」。切换时同步发起请求，这里只等回调。
            pump(app, lambda: not w._db_pending, 15)
            settle(app, max_s=3.0)
            pm = page.grab()
            if not pm.isNull():
                pm.save(str(OUT / f"{attr}.png"))
        print(f"[{THEME}] {win_w}x{win_h} 已截图 -> {OUT}")
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
