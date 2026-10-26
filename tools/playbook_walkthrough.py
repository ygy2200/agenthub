# -*- coding: utf-8 -*-
"""编排中心真实库走查（R170）：五段空态/选中联动/按钮上下文。

用法：python tools/playbook_walkthrough.py [root] [输出后缀]
产出：output/real-playbook<后缀>/ 下 flows/steps/runs/triggers/graph/no-root.png
"""
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))


def _shot(app, pb, path, settle):
    """分段指示条是 ~250ms 动画；事件队列安静不代表动画结束，截图前等它落位。"""
    settle(app, max_s=0.5)
    time.sleep(0.35)
    settle(app, max_s=0.3)
    pb.grab().save(str(path))


def _empty_text(pb):
    from PySide6.QtWidgets import QLabel
    for box in pb._empty_widgets.values():
        if box.isVisible():
            return " / ".join(lb.text() for lb in box.findChildren(QLabel))
    return ""


def _actions(pb):
    return (f"跑一次={'可用' if pb.runBtn.isEnabled() else '灰'} "
            f"续跑={'可用' if pb.resumeBtn.isEnabled() else '灰'} "
            f"中止={'可用' if pb.abortBtn.isEnabled() else '灰'} "
            f"启停={'可用' if pb.togBtn.isEnabled() else '灰'} "
            f"建边={'可用' if pb.buildBtn.isEnabled() else '灰'}")


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
    out = ROOT / "output" / f"real-playbook{suffix}"
    try:
        w.resize(1280, 820)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        w.switchTo(w.playbook_page)
        pb = w.playbook_page
        assert pump(app, lambda: pb.isVisible() and bool(pb._flows), 20), "流程段未加载"
        settle(app, max_s=1.6)
        out.mkdir(parents=True, exist_ok=True)

        print(f"流程：{len(pb._flows)} 条 · {_actions(pb)}", flush=True)
        pb.listw.setCurrentRow(0)
        settle(app, max_s=0.4)
        print(f"选中流程：{pb.cur_flow} · {_actions(pb)}", flush=True)
        _shot(app, pb, out / "flows.png", settle)

        pb.seg.setCurrentItem("steps")
        settle(app, max_s=0.8)
        head = pb.listw.item(0).text() if pb.listw.count() else "（无）"
        print(f"步骤：列表可见={pb.listw.isVisible()} 行数={pb.listw.count()} 首行={head}"
              f" · {_actions(pb)}", flush=True)
        _shot(app, pb, out / "steps.png", settle)

        pb.seg.setCurrentItem("runs")
        settle(app, max_s=0.8)
        print(f"执行历史：行数={pb.listw.count()} 空态={_empty_text(pb) or '无'}"
              f" · {_actions(pb)}", flush=True)
        if pb.listw.count():
            pb.listw.setCurrentRow(0)
            done = pump(app, lambda: pb.listw.currentItem() is not None
                        and pb.listw.currentRow() == 0, 2)
            marks = [pb.listw.item(i).text().strip()[:24]
                     for i in range(min(pb.listw.count(), 6))]
            print(f"选中执行 #{pb.cur_run}：保持选中={done} · {_actions(pb)}", flush=True)
            for m in marks:
                print(f"  {m}", flush=True)
        _shot(app, pb, out / "runs.png", settle)

        pb.seg.setCurrentItem("trig")
        settle(app, max_s=0.8)
        print(f"触发器：行数={pb.listw.count()} 空态={_empty_text(pb) or '无'}"
              f" · {_actions(pb)}", flush=True)
        _shot(app, pb, out / "triggers.png", settle)

        pb.seg.setCurrentItem("graph")
        settle(app, max_s=1.2)
        head = pb.listw.item(0).text() if pb.listw.count() else "（无）"
        print(f"图谱：列表可见={pb.listw.isVisible()} 行数={pb.listw.count()} 空态={_empty_text(pb) or '无'}",
              flush=True)
        print(f"  {head}", flush=True)
        print(f"  {_actions(pb)}", flush=True)
        _shot(app, pb, out / "graph.png", settle)

        w.root = None
        pb.reload()
        settle(app, max_s=0.4)
        print(f"无根目录：空态={_empty_text(pb) or '无'} · {_actions(pb)}", flush=True)
        _shot(app, pb, out / "no-root.png", settle)
        print(f"截图目录：{out}", flush=True)
    finally:
        core.get_root = orig
        w.close()


if __name__ == "__main__":
    main()
