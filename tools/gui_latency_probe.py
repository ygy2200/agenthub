# -*- coding: utf-8 -*-
"""载入态走查 · 切页首帧量具（R763，B005 第三步）。

问题：R238 的口径是「上屏时页面上必须有东西：数据或忙态，绝不是一屏 0/空白」。
但只有统计页/大脑页做成了异步 + 骨架屏；其余数据页在 `switchTo` 里**先同步读库、
再让页面上屏**——库一大，用户点了菜单却没有任何视觉响应（帧都没换）。

量什么：
  · 同步阻塞 ms：`w.switchTo(page)` 在 UI 线程上占用的时间（这段时间窗口不重绘）
  · 忙态：切换返回后立刻看页面上有没有骨架屏（Skeleton）或「正在读取」文案
  · 总耗时：含异步取数完成的时间

判据：
  · 同步阻塞 > 300ms 且无忙态 → 缺陷（用户点了没反应）
  · 异步页（_DB_ASYNC）切换后没有忙态 → 缺陷（会白屏一帧）
  · 同步阻塞 100–300ms → 观察项

用法：
    python tools/gui_latency_probe.py [--records 800] [--size 920x600] [--out <目录>]

退出码：0 = 无缺陷；1 = 有缺陷；2 = 环境不可用。
"""
import os
import shutil
import sys
import time
from pathlib import Path

import _console  # noqa: E402,F401  控制台 UTF-8 守卫

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))

SYNC_DEFECT_MS = 300.0
SYNC_WARN_MS = 100.0


def _split(argv):
    opts, i = {}, 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                k, v = a.split("=", 1)
            elif i + 1 < len(argv):
                opts[a] = argv[i + 1]
                i += 1
            else:
                opts[a] = ""
        i += 1
    return opts


OPTS = _split(sys.argv[1:])


def _load_production_font():
    from PySide6.QtGui import QFontDatabase

    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"):
        if os.path.exists(path):
            QFontDatabase.addApplicationFont(path)
            return path
    return ""


def _busy_visible(page) -> bool:
    """页面上有没有真忙态：骨架屏控件或「正在读取」文案（R239 的两条同一口径）。"""
    from PySide6.QtWidgets import QLabel, QWidget

    try:
        from uikit import Skeleton
    except Exception:  # noqa: BLE001
        Skeleton = None
    if Skeleton is not None:
        for c in page.findChildren(QWidget):
            if isinstance(c, Skeleton) and c.isVisible():
                return True
    for c in page.findChildren(QLabel):
        if c.isVisible() and "正在读取" in (c.text() or ""):
            return True
    return False


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    size = OPTS.get("--size", "920x600")
    win_w, win_h = (int(x) for x in size.lower().split("x"))
    nrec = int(OPTS.get("--records", "800"))
    root = Path(OPTS.get("--root") or ROOT / "_pytest_tmp" / "gui-latency-r763")
    if root.resolve().is_relative_to((ROOT / "_pytest_tmp").resolve()) and root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    out = Path(OPTS.get("--out") or ROOT / "output" / "gui-latency-r763")
    out.mkdir(parents=True, exist_ok=True)

    from PySide6.QtWidgets import QApplication

    import actions
    import brain
    import core
    import tokens
    import ui
    from helpers import insert_records, pump, settle

    app = QApplication.instance() or QApplication([])
    _load_production_font()
    tokens.apply_theme("dark")
    core.get_root = lambda: str(root)
    core.save_config = lambda *a, **k: None

    ntask = int(OPTS.get("--tasks", "200"))
    nsig = int(OPTS.get("--signals", "200"))
    core.init_hub(str(root))
    brain.init_db(str(root))
    insert_records(str(root), nrec, projects=8)
    # 真实的大脑不只长 records：任务队列与信号也会积压，工作台/信号中心的读取路径要一起量
    for i in range(ntask):
        brain.task_add(str(root), f"任务 {i}", project=f"P{i % 8}", agent="zcode")
    for i in range(nsig):
        brain.signal_send(str(root), "zcode", f"消息 {i}")

    w = ui.AgentHubWindow()
    w._skip_close_hooks = True
    problems, warns, rows = [], [], []
    try:
        w.resize(win_w, win_h)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        settle(app, max_s=4.0)
        db_pages = set(w._DB_PAGES)
        async_pages = set(w._DB_ASYNC)
        known = {a: t for a, t, *_ in actions.PAGES}

        for attr, title, *_rest in actions.PAGES:
            if attr not in db_pages:
                continue
            page = getattr(w, attr, None)
            if page is None:
                problems.append(f"{attr}：页面不存在")
                continue
            w._db_dirty = {attr}
            t0 = time.perf_counter()
            w.switchTo(page)
            sync_ms = (time.perf_counter() - t0) * 1000
            busy = _busy_visible(page)
            total_ms = sync_ms
            if attr in async_pages:
                t1 = time.perf_counter()
                pump(app, lambda a=attr: a not in w._db_pending, 8)
                total_ms = sync_ms + (time.perf_counter() - t1) * 1000
            settle(app, max_s=2.0)
            try:
                page.grab().save(str(out / f"{attr}.png"))
            except Exception:  # noqa: BLE001
                pass
            state = "异步" if attr in async_pages else "同步"
            if attr in async_pages and not busy:
                problems.append(f"{attr}：异步页切换后没有忙态（会先白屏一帧）")
            elif attr not in async_pages and sync_ms > SYNC_DEFECT_MS:
                problems.append(f"{attr}：同步阻塞 {sync_ms:.0f}ms > {SYNC_DEFECT_MS:.0f}ms"
                                "（点菜单后窗口不重绘，用户以为没点上）")
            elif attr not in async_pages and sync_ms > SYNC_WARN_MS:
                warns.append(f"{attr}：同步阻塞 {sync_ms:.0f}ms（> {SYNC_WARN_MS:.0f}ms，"
                             "数据再多就要变硬冻）")
            rows.append((attr, known.get(attr, title), state, sync_ms, total_ms, busy))
    finally:
        try:
            w._skip_close_hooks = True
            w.close()
        except Exception:  # noqa: BLE001
            pass

    print(f"切页首帧 @ {win_w}x{win_h}  记录 {nrec} 条  根 {root}")
    print(f"{'页面':<16}{'名称':<10}{'方式':<6}{'同步阻塞':>10}{'总耗时':>10}  忙态")
    for attr, title, state, sync_ms, total_ms, busy in rows:
        print(f"{attr:<16}{title:<10}{state:<6}{sync_ms:>8.0f}ms{total_ms:>8.0f}ms  "
              f"{'有' if busy else '无'}")
    print(f"截图：{out}（{len(rows)} 页）")
    if warns:
        print(f"\n观察项 {len(warns)} 条（不判失败）：")
        for x in warns:
            print("  - " + x)
    if problems:
        print(f"\n✗ {len(problems)} 处缺陷：")
        for p in problems:
            print("  - " + p)
        return 1
    print("\n✓ 全部数据页载入态通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
