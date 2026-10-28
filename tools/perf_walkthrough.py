"""R209 性能走查：真实库下各页首次打开的主线程最长阻塞（>1s 即体验问题）。

量法：QTimer 每 10ms 心跳，记录切换后 800ms 窗口内的最大间隔（= 用户可感知的卡顿）。
旧实现直接调 `page.reload()`，但多数页面没有该方法；随后用「事件循环安静」判据
会把后台定时器误判成 5s 慢页。心跳法不受这两类假象影响。
"""
import os
import sys
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))

SLOW_MS = 1000
WINDOW_S = 0.8


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    real_root = r"D:\agent资料\zcode项目记录"
    from PySide6.QtCore import QObject, QTimer
    from PySide6.QtWidgets import QApplication
    import actions
    import core
    import tokens
    import ui

    app = QApplication.instance() or QApplication([])
    tokens.apply_theme("dark")
    core.get_root = lambda: real_root
    t0 = time.perf_counter()
    w = ui.AgentHubWindow()
    init = time.perf_counter() - t0

    class Beat(QObject):
        def __init__(self):
            super().__init__()
            self.last = time.perf_counter()
            self.gaps = []
            self.t = QTimer(self)
            self.t.setInterval(10)
            self.t.timeout.connect(self.tick)

        def tick(self):
            now = time.perf_counter()
            self.gaps.append(now - self.last)
            self.last = now

        def reset(self):
            self.gaps.clear()
            self.last = time.perf_counter()

    beat = Beat()
    beat.t.start()
    # 显式等窗口首个全局刷新（ScanWorker 后台扫描完成后才 apply，落在 1–3s 后）：
    # 以前只静默 0.3s 就开测，启动刷新会随机落进「总览」的 800ms 窗口，
    # 表现为间歇性 2.4s 假慢页（R217 根因之一）。
    deadline = time.perf_counter() + 20
    while time.perf_counter() < deadline and not getattr(w, "_apply_count", 0):
        app.processEvents()
        time.sleep(0.005)
    idle_end = time.perf_counter() + 0.3
    while time.perf_counter() < idle_end:
        app.processEvents()
        time.sleep(0.005)
    while beat.gaps and max(beat.gaps) > 0.05:
        beat.reset()
        idle_end = time.perf_counter() + 0.3
        while time.perf_counter() < idle_end:
            app.processEvents()
            time.sleep(0.005)
    print(f"窗口初始化：{init:.2f}s（含首次 refresh）")

    rows, seen = [], set()
    for attr, title, *_ in actions.PAGES:
        page = getattr(w, attr, None)
        if page is None or id(page) in seen:
            continue
        seen.add(id(page))
        beat.reset()
        w.switchTo(page)
        end = time.perf_counter() + WINDOW_S
        while time.perf_counter() < end:
            app.processEvents()
            time.sleep(0.002)
        gaps = beat.gaps or [0.0]
        rows.append((title, max(gaps) * 1000, sum(gaps[1:]) * 1000))

    for title, gap, busy in sorted(rows, key=lambda r: -r[1]):
        flag = " ⚠慢" if gap > SLOW_MS else ""
        print(f"  {title}: 最长阻塞 {gap:.0f}ms · 总忙 {busy:.0f}ms{flag}")
    w.close()
    slow = [t for t, gap, _ in rows if gap > SLOW_MS]
    print(f"共 {len(rows)} 页，卡顿页(>{SLOW_MS}ms) {len(slow)} 个" + (f"：{'、'.join(slow)}" if slow else ""))
    return 1 if slow else 0


if __name__ == "__main__":
    sys.exit(main())
