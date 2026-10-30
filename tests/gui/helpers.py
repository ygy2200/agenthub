# -*- coding: utf-8 -*-
"""gui 层公用：事件泵 / 临时库行注入 / 窗口启动。"""
import time


def pump(app, cond, timeout=15.0):
    """处理事件直到 cond 为真或超时。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.002)
    return False


def settle(app, max_s=8.0):
    """等到事件队列安静（连续 5 轮单轮 < 4ms）。"""
    quiet = 0
    t0 = time.time()
    while time.time() - t0 < max_s and quiet < 5:
        t = time.time()
        app.processEvents()
        quiet = quiet + 1 if time.time() - t < 0.004 else 0
        time.sleep(0.002)


def insert_records(root, n, projects=5):
    """直插 records（绕开语义钩子，压测/渲染测试专用）。"""
    import brain
    with brain.db_conn(root) as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO records(project,agent,date,title,content,source,created,"
                "valid_from,record_type) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"P{i % projects}", "zcode", f"2026-10-{1 + i % 9:02d}", f"r{i}", f"c{i}",
                 "hub", "2026-10-11T00:00:00", "2026-10-11", "work_log"))
        for k in range(projects):
            conn.execute("INSERT OR IGNORE INTO projects(name,created) VALUES(?,?)",
                         (f"P{k}", "2026-10-11"))


def dead_path(tag="dead"):
    """返回一个确定不存在的路径。

    优先落在**不存在的盘符**上（真实「移动硬盘拔了/盘符变了」场景）；CI 或本机恰好
    所有候选盘都存在时，退回临时目录下不存在的子目录——两者走的是同一判定分支
    （`os.path.isdir(root)` 为假），不会因为机器盘符不同而让用例失去意义。
    """
    import os
    import tempfile
    import uuid
    suffix = f"__agenthub_{tag}_{uuid.uuid4().hex[:6]}"
    for letter in "ZYXQRT":
        if not os.path.exists(f"{letter}:/"):
            return f"{letter}:/{suffix}"
    return os.path.join(tempfile.gettempdir(), suffix)


def boot_window(qapp, root):
    """构造真实窗口（core.get_root 已由 hub_root fixture patch 指向临时库）。

    测试窗口一律走无副作用关闭：closeEvent 正常路径会备份大脑、存配置并排一个
    2s 硬退定时器（os._exit(0)）——同进程的 pytest 会被它整个杀掉。"""
    import ui
    w = ui.AgentHubWindow()
    w._skip_close_hooks = True
    w.resize(1280, 820)
    w.show()
    _WINDOWS.append(w)
    return w


_WINDOWS = []


def cleanup_windows(app=None):
    """用例边界销毁窗口（R221）。

    用例只在 finally 里 `w.hide()`，窗口本身活到进程结束——106 个用例在同一进程里
    堆出上百个窗口和后台扫描线程（实测跑到 26% 已 2.9GB / 90 线程、CPU 打满），
    事件循环越跑越慢，套件从秒级变成二十分钟级。关闭 + deleteLater + 冲刷
    DeferredDelete，让每个用例从干净状态开始。

    只在**全部后台线程已结束**时才删：QThread 带任务时被析构会让整个进程进入
    半死状态（实测后续出现 `QFile.open → SystemError: bad argument to internal
    function` 这类毫不相干的报错）。等不到就只隐藏并保留引用——内存仍比不删低
    （不可见窗口不参与绘制），但绝不在线程在飞时销毁。
    """
    import gc
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QWidget
    kept = []
    for w in _WINDOWS:
        try:
            if not _workers_idle(w):
                kept.append(w)
                continue
            _deregister_styles(w, QWidget)
            w.close()
            w.deleteLater()
        except RuntimeError:  # 已被 Qt 销毁
            pass
    _WINDOWS[:] = kept
    if app is not None:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    gc.collect()


def _workers_idle(w, budget_s=1.0):
    """窗口及其各页的后台线程是否都已结束（未结束的只断信号，不销毁窗口）。"""
    try:
        lists = w._collect_worker_lists()
    except Exception:  # noqa: BLE001
        return True
    idle = True
    for lst in lists:
        for t in list(lst):
            try:
                t.blockSignals(True)
                if t.isRunning() and not t.wait(int(budget_s * 1000)):
                    idle = False
            except RuntimeError:
                pass
    return idle


def _deregister_styles(w, QWidget):
    try:
        from qfluentwidgets.common.style_sheet import styleSheetManager
    except Exception:  # noqa: BLE001 qfw 换版本时清理照常进行
        return
    for x in [w] + w.findChildren(QWidget):
        try:
            styleSheetManager.deregister(x)
        except Exception:  # noqa: BLE001
            pass
