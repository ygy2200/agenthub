# -*- coding: utf-8 -*-
"""gui 层：延后回调的生命周期守卫（R239·门禁实测崩在 teardown 之后补的钉）。

现象（`output/_r239_gate1.txt`）：全量 GUI 门禁里 `test_typography_guard.py`
**5 passed, 1 error**——teardown 报
`RuntimeError: libshiboken: Internal C++ object (ScrollArea) already deleted`，
栈顶 `pages/hub_page.py:366 _fit_cards_height`；**单跑该文件 5 passed 不复现**。
定罪：`QTimer.singleShot(0, self._fit_cards_height)` 这类「静态单次定时器 + 绑定方法」
的定时器**不属于任何对象**，页面/窗口被销毁后它照样在事件循环里到期，回调撞上
「Python 包装还在、C++ 对象已删」。遍历全部页面的守卫测试里，切页顺序 + 300ms 首扫回调
与拆窗（helpers.cleanup_windows 会冲刷 DeferredDelete 真正销毁控件）的相对时序刚好错开，
所以只在整层跑时炸。

这不是新坑：R199 已为接入页定过同一条规矩（`pages/connect_page.py:90` 注释），
`window.py:174` 也把它写成文档——「QTimer 挂 self 为 parent——窗口销毁时定时器随之
销毁，不会在死对象上触发」。本轮把统计页取数挪到后台之后，全窗事件节奏变了，
把其余还没守规矩的 7 处调用点暴露出来。

本文件钉四条：
  ① 源码静态守卫：应用代码里不得再出现 `singleShot(<ms>, self.xxx)` / `singleShot(<ms>, lambda…)`；
  ② 页面销毁后直接调用延后回调必须静默返回（不是抛 RuntimeError）；
  ③ 反向对照：旧写法（静态 singleShot + 绑定方法）在页面销毁后确实会在事件循环里抛
     RuntimeError——证明①不是故弄玄虚的写法洁癖；
  ④ 修完不许「干脆不排版」：卡片区仍要按整行定高（防把延后调用删掉了事）。
"""
import os
import re
import sys

import pytest

from helpers import boot_window, pump

APP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 应用代码范围（一次性脚本 tools/、diag_launch.py、smoke_ui.py 不在内：它们自己管生命周期）
SCOPE = ["window.py", "uikit.py", "palette.py"] + [
    os.path.join("pages", fn) for fn in sorted(os.listdir(os.path.join(APP, "pages")))
    if fn.endswith(".py")
]

# 静态单次定时器 + 「绑定到某个对象的方法/闭包」= 销毁后照样到期的风险写法。
# 裸函数名（window.py 关窗 2s 强制退出看门狗）不在内：它故意不随窗口寿命作废。
BAD = re.compile(r"QTimer\.singleShot\(\s*\d+\s*,\s*(?:self\.|lambda\b|\w+\.\w+)")


def _reap(qapp, w):
    """按 R221 的既有口径真正销毁窗口及其全部子控件（deleteLater 只排队，要手动冲刷）。

    先等在飞线程清空：QThread 带任务时析构所属窗口会把整个进程打进半死状态（R221 实测）。
    """
    from helpers import _workers_idle
    from PySide6.QtCore import QCoreApplication, QEvent
    assert pump(qapp, lambda: _workers_idle(w), timeout=20.0), "后台线程未结束，不能销毁窗口"
    w.close()
    w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def _hub_ready(qapp, w):
    hub = w.hub_page
    assert pump(qapp, lambda: hub.cardsRow.count() > 0, timeout=20.0), "卡片未建成，无法验证"
    return hub


def test_no_unparented_single_shot_bound_to_self():
    """延后回调必须由**挂在对象上的 QTimer** 承载（R199 规矩），静态守卫一次扫清。"""
    missing = [rel for rel in SCOPE if not os.path.isfile(os.path.join(APP, rel))]
    assert not missing, f"守卫扫描清单指向了不存在的路径（扫描面会静默缩水）：{missing}"
    assert len(SCOPE) >= 20, f"守卫扫描面异常小（{len(SCOPE)} 个文件），路径基准 APP 可能算错"
    hits = []
    for rel in SCOPE:
        p = os.path.join(APP, rel)
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if BAD.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()}")
    assert not hits, (
        "静态单次定时器不属于任何对象，销毁后照样到期并撞上已删除的 C++ 控件；"
        "改成 QTimer(self, singleShot=True) + timeout.connect：\n" + "\n".join(hits))


def test_deferred_fit_is_silent_after_page_destroyed(qapp, hub_root):
    """页面已销毁（包装还在）时直接调延后回调，必须静默返回而不是抛 RuntimeError。"""
    import shiboken6

    w = boot_window(qapp, hub_root)
    hub = _hub_ready(qapp, w)
    _reap(qapp, w)
    assert pump(qapp, lambda: not shiboken6.isValid(hub), timeout=10.0), \
        "页面 C++ 对象未销毁，本用例没有验到死对象场景"
    try:
        hub._fit_cards_height()
    except RuntimeError as e:
        pytest.fail(f"死页面上的延后回调仍然触碰已删除控件：{e}")


def test_static_single_shot_on_dead_page_really_crashes(qapp, hub_root):
    """反向对照：静态 singleShot 排的回调在页面销毁后**确实会执行**并撞上死控件——
    ①的静态守卫必须有真实故障撑着，不是写法洁癖。

    这里刻意直接触碰子控件（不经 `_fit_cards_height` 的判活闸），因为本轮已给该回调
    加了幂等保护；对照要证的是「定时器不属于任何对象 → 销毁后照样到期」这一半。
    """
    import shiboken6
    from PySide6.QtCore import QTimer

    seen = []
    saved = sys.excepthook

    def hook(etype, value, tb):
        seen.append(f"{etype.__name__}: {value}")

    w = boot_window(qapp, hub_root)
    hub = _hub_ready(qapp, w)
    _reap(qapp, w)
    assert not shiboken6.isValid(hub), "页面未销毁，对照组失效"
    QTimer.singleShot(0, lambda: hub.cardsScroll.viewport().width())  # 复刻被禁的写法
    sys.excepthook = hook
    try:
        for _ in range(30):
            qapp.processEvents()
    finally:
        sys.excepthook = saved
    assert any("RuntimeError" in s for s in seen), (
        "静态定时器在页面销毁后没有执行回调——要么 PySide 行为变了，要么本对照失效，"
        "此时①的静态守卫需要重新论证")


def test_card_area_still_snaps_to_whole_rows(qapp, hub_root):
    """反向闸：不许用「不再排版」来消除崩溃——卡片区仍要按整行高度定高。"""
    w = boot_window(qapp, hub_root)
    w.switchTo(w.hub_page)
    hub = _hub_ready(qapp, w)
    row_h = max((hub.cardsRow.itemAt(i).widget().sizeHint().height()
                 for i in range(hub.cardsRow.count())
                 if hub.cardsRow.itemAt(i).widget() is not None), default=0)
    gap = getattr(hub.cardsRow, "_v", 12)
    assert pump(qapp, lambda: hub.cardsScroll.height() in (row_h, 2 * row_h + gap),
                timeout=15.0), (
        f"卡片区高度始终不落整行（行高 {row_h}、间距 {gap}、当前 {hub.cardsScroll.height()}）"
        "——延后定高被改成不生效了")
