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


def boot_window(qapp, root):
    """构造真实窗口（core.get_root 已由 hub_root fixture patch 指向临时库）。"""
    import ui
    w = ui.AgentHubWindow()
    w.resize(1280, 820)
    w.show()
    return w
