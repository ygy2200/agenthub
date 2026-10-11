# -*- coding: utf-8 -*-
"""P2-3a 压测：注入 N 条记录（默认 5000），量测时间线虚拟滚动各环节耗时。

用法：python tools/bench_timeline.py [N]
输出：注入耗时 / 首页就绪 / 逐页加载（含控件渲染）/ 滚动到底自动加载 / box 规模。
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402

tmp = tempfile.mkdtemp(prefix="bench_tl_")
core.get_root = lambda: tmp  # 窗口构造前 patch，防触碰真实库

import brain  # noqa: E402

brain.init_db(tmp)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 5000

t0 = time.time()
with brain.db_conn(tmp) as conn:
    for i in range(N):
        conn.execute(
            "INSERT INTO records(project,agent,date,title,content,source,created,valid_from,record_type) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (f"压测项目{i % 8}", "zcode", f"2026-{1 + i % 9:02d}-{1 + i % 27:02d}",
             f"压测记录{i}", f"内容{i}", "hub", "2026-10-11T00:00:00", "2026-10-11", "work_log"))
    for k in range(8):
        conn.execute("INSERT OR IGNORE INTO projects(name,created) VALUES(?,?)",
                     (f"压测项目{k}", "2026-10-11"))
print(f"注入 {N} 条：{time.time() - t0:.2f}s", flush=True)

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
import tokens  # noqa: E402
import ui  # noqa: E402

tokens.apply_theme("dark")
w = ui.AgentHubWindow()
w.resize(1400, 900)
w.show()
tl = w.timeline_page
w.switchTo(tl)  # 必须切到时间线页：不可见页不参与布局，滚动条 range 不更新（防"max=0"假象）


def pump(cond, timeout=60.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return False


t0 = time.time()
ok = pump(lambda: tl._vstate and tl._vstate["offset"] >= 100 and not tl._vstate["loading"])
print(f"首页（{tl.render_limit if not tl._virtual else 100} 条/{'虚拟' if tl._virtual else '旧'}模式）就绪："
      f"{time.time() - t0:.3f}s（{ok}）", flush=True)

for _ in range(30):  # 让布局稳定（滚动条 range 需要布局完成）
    app.processEvents()
    time.sleep(0.01)

times = []
for _k in range(4):
    before = tl._vstate["offset"]
    t0 = time.time()
    tl._vfetch()
    pump(lambda: tl._vstate["offset"] > before, 30)
    times.append(time.time() - t0)
print("逐页加载（含控件渲染）：", " / ".join(f"{t:.3f}s" for t in times), flush=True)

bar = tl.scroll.verticalScrollBar()
print("滚动条 max:", bar.maximum(), "| 已加载:", tl._vstate["offset"], "/", tl._vstate["total"], flush=True)
before = tl._vstate["offset"]
t0 = time.time()
bar.setValue(bar.maximum())
ok2 = pump(lambda: tl._vstate["offset"] > before, 15)
print(f"滚动到底自动加载：{'触发成功' if ok2 else '未触发'}  {time.time() - t0:.3f}s → "
      f"offset={tl._vstate['offset']}", flush=True)

# 连续滚动到底 x3：验证"滚动即加载"在真实布局下持续工作
trig = []
for _i in range(3):
    if tl._vstate["offset"] >= tl._vstate["total"]:
        break
    before = tl._vstate["offset"]
    bar.setValue(bar.maximum())
    ok3 = pump(lambda: tl._vstate["offset"] > before, 15)
    trig.append("✓" if ok3 else "✗")
print("连续滚底触发：", " ".join(trig) if trig else "(已全部加载)", flush=True)
print("box 顶层项数（日期组+尾部）:", tl.box.count(), flush=True)
os._exit(0)
