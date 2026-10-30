# -*- coding: utf-8 -*-
"""读失败走查 · 错态横扫（R762，B005 第二步）。

背景：R756 把「空态」做成了可复跑量具，错态仍靠手感。R239 只给大脑页/统计页做了
忙态与失败占位；其余页面读库失败时走 window._reload_db_page 的 except（静默、保留
脏标记），页面本身不吭声——用户看到的可能是「还没有数据」的空态，于是把「读不出来」
误读成「库里没东西」。

做法：先用**种子库**（12 条记录 + 4 个项目）正常建窗，逐页记下基线（空态标题/列表行数）；
再把数据层打瘸（brain 的公开函数 + core.scan 全部抛错），标脏后逐页切过去，对每页判定：
  · 崩溃：切页/刷新抛异常（页面级 try 之外的）
  · 失败提示：页面上出现「失败/错误/无法…」文案
  · 假空态：基线里有内容、读失败后却变成空态 —— 最危险的一类（把故障说成没数据）
  · 空白：页面上没有任何可见文字
  · 保留旧内容：仍有文字但无失败提示（观察项，不判失败）
只对 `window._DB_PAGES`（切页时会重读脑库的 7 页）要求「必须出现可见失败提示」：
窗口级 InfoBar 或页内文案都算；失败文案生成后被空态盖掉、静默保留旧内容、显示假空态
都判失败。报告/搜索这类非数据页的数据来自窗口快照，不在此列。
另外检查失败提示是否带下一步（重试/刷新/下次/检查/设置/根目录/初始化）。

为什么要有基线：空库上「暂无记录」本来就对，不能算假空态；只有「本来有、读失败后没了」
才是把故障说成没数据。首版在空库上跑，overview/project/governance 全被误判过。

用法：
    python tools/gui_error_sweep.py [--root <空目录>] [--out <截图目录>] [--size 920x600]

退出码：0 = 无缺陷（假空态/空白/崩溃）；1 = 有缺陷；2 = 环境不可用。
"""
import os
import re
import shutil
import sys
from pathlib import Path

import _console  # noqa: E402,F401  控制台 UTF-8 守卫

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))

# 静态页不读脑库：打瘸数据层后仍应正常显示，不要求失败提示
NO_DATA_EXPECTED = {"help_page", "components_page", "settings_page", "connect_page"}
FAIL_WORDS = re.compile(r"失败|错误|无法读取|读不出来|出错|异常")
NEXT_STEP = re.compile(r"重试|刷新|下次|检查|设置|根目录|初始化|F5")


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


def _visible_texts(page):
    """页面上所有可见文案。检测不能截断：失败占位常是后加入的控件，
    首版 limit=8 把「大脑加载失败：…」整段截掉，异步页被误报成「保留旧内容」。"""
    from PySide6.QtWidgets import QLabel, QPushButton

    out = []
    for cls in (QLabel, QPushButton):
        for c in page.findChildren(cls):
            if not c.isVisible():
                continue
            t = (c.text() or "").strip()
            if t and t not in out:
                out.append(t)
    return out


def _all_texts(widget):
    """含隐藏控件：用来抓「失败文案生成了又被藏起来」（governance 的 R762 缺陷）。"""
    from PySide6.QtWidgets import QLabel, QPushButton

    out = []
    for cls in (QLabel, QPushButton):
        for c in widget.findChildren(cls):
            t = (c.text() or "").strip()
            if t and t not in out:
                out.append(t)
    return out


def _chrome_text(win, page) -> str:
    """窗口里不属于当前页的可见文案（InfoBar 等窗口级提示落在这里）。"""
    from PySide6.QtWidgets import QLabel, QPushButton

    out = []
    for cls in (QLabel, QPushButton):
        for c in win.findChildren(cls):
            if not c.isVisible() or page.isAncestorOf(c):
                continue
            t = (c.text() or "").strip()
            if t and t not in out:
                out.append(t)
    return " ".join(out)


def classify(attr, db_page, no_data, visible_fail, hidden_fail, empties, blob, rows_before):
    """把一页的观察结果归成 (状态, 缺陷说明/None, 观察项/None)。

    纯函数，门禁用例靠它做反证：量具若永远返回通过，等于没有量具。
    """
    if no_data:
        return "静态页", None, None
    if not db_page:
        return "非数据页", None, None
    if visible_fail:
        warn = f"{attr}：失败提示没有下一步（{blob[:50]}）" if not NEXT_STEP.search(blob) else None
        return "失败提示", None, warn
    if hidden_fail:
        return ("失败被藏",
                f"{attr}：失败文案已生成却被藏起来（{hidden_fail[0][:50]}），"
                f"页面现显示：{blob[:40] or '无'}", None)
    if empties:
        return ("假空态",
                f"{attr}：读失败后显示空态「{empties[0]}」"
                f"（基线 {rows_before} 行）——用户会以为库里没东西", None)
    return "静默失败", f"{attr}：读失败无任何提示，仍显示旧内容（{blob[:40] or '空白'}）", None


def _list_rows(page) -> int:
    """列表/表格真实条目数（与 gui_empty_sweep 同口径）。"""
    from PySide6.QtWidgets import QListWidget, QTableWidget, QTreeWidget

    def real(text):
        t = (text or "").strip()
        return bool(t) and not (t.startswith("（") and t.endswith("）"))

    n = 0
    for cls in (QListWidget, QTreeWidget, QTableWidget):
        for w in page.findChildren(cls):
            if not w.isVisible():
                continue
            if isinstance(w, QTableWidget):
                n += sum(1 for r in range(w.rowCount())
                         if w.item(r, 0) is not None and real(w.item(r, 0).text()))
            elif isinstance(w, QTreeWidget):
                n += sum(1 for i in range(w.topLevelItemCount())
                         if real(w.topLevelItem(i).text(0)))
            else:
                n += sum(1 for i in range(w.count()) if real(w.item(i).text()))
    return n


def _snapshot(page):
    texts = _visible_texts(page)
    return {"texts": texts, "blob": " ".join(texts),
            "empties": _empty_titles(page), "rows": _list_rows(page)}


def _empty_titles(page):
    from PySide6.QtWidgets import QWidget

    return [c.titleLabel.text().strip() for c in page.findChildren(QWidget)
            if hasattr(c, "titleLabel") and c.isVisible()
            and (c.titleLabel.text() or "").strip()]


def _sabotage():
    """把脑库读入口全部换成抛错。返回可恢复的 (module, name, 原函数) 列表。"""
    import inspect

    import brain
    import core

    def boom(*a, **k):
        raise RuntimeError("模拟读取失败（gui_error_sweep）")

    saved = []
    for name, fn in list(vars(brain).items()):
        if name.startswith("_") or not inspect.isfunction(fn):
            continue
        if getattr(fn, "__module__", "") != brain.__name__:
            continue
        saved.append((brain, name, fn))
        setattr(brain, name, boom)
    saved.append((core, "scan", core.scan))
    core.scan = boom
    return saved


def _restore(saved):
    for mod, name, fn in saved:
        setattr(mod, name, fn)


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    size = OPTS.get("--size", "920x600")
    win_w, win_h = (int(x) for x in size.lower().split("x"))
    root = Path(OPTS.get("--root") or ROOT / "_pytest_tmp" / "gui-error-r762")
    if root.resolve().is_relative_to((ROOT / "_pytest_tmp").resolve()) and root.exists():
        shutil.rmtree(root, ignore_errors=True)   # 自建临时根：每跑一次从干净种子开始
    root.mkdir(parents=True, exist_ok=True)
    out = Path(OPTS.get("--out") or ROOT / "output" / "gui-error-r762")
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
    core.save_config = lambda *a, **k: None   # 走查永不写用户配置（R232/R756 同款）

    # 种子库：没有基线就分不清「本来就是空」和「读失败被说成空」
    core.init_hub(str(root))
    brain.init_db(str(root))
    insert_records(str(root), 12, projects=4)

    w = ui.AgentHubWindow()
    w._skip_close_hooks = True
    problems, warns, rows = [], [], []
    saved = []
    try:
        w.resize(win_w, win_h)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        base = {}
        for attr, title, *_rest in actions.PAGES:
            page = getattr(w, attr, None)
            if page is None:
                continue
            w.switchTo(page)
            if hasattr(w, "_db_pending"):
                pump(app, lambda a=attr: a not in w._db_pending, 5)
            settle(app, max_s=3.0)
            base[attr] = _snapshot(page)
        saved = _sabotage()
        db_pages = set(getattr(w, "_DB_PAGES", ()))
        for attr, title, *_rest in actions.PAGES:
            page = getattr(w, attr, None)
            if page is None:
                problems.append(f"{attr}：页面不存在")
                rows.append((attr, title, "缺失", ""))
                continue
            try:
                if attr in db_pages:
                    # 每页都重新标脏：治理页切段会改兄弟段的脏标记，只设一次会漏
                    w._db_dirty = set(w._DB_PAGES)
                w.switchTo(page)
                if hasattr(w, "_db_pending"):
                    pump(app, lambda a=attr: a not in w._db_pending, 5)
                settle(app, max_s=3.0)
            except Exception as e:  # noqa: BLE001 单页崩了要继续走完其余页
                problems.append(f"{attr}：切页异常 {type(e).__name__}: {e}")
                rows.append((attr, title, "崩溃", str(e)[:40]))
                continue
            snap = _snapshot(page)
            blob, empties = snap["blob"], snap["empties"]
            before = base.get(attr) or {"texts": [], "blob": "", "empties": [], "rows": 0}
            shot = out / f"{attr}.png"
            try:
                page.grab().save(str(shot))
            except Exception:  # noqa: BLE001
                pass
            chrome = _chrome_text(w, page)
            hidden_fail = [t for t in _all_texts(page)
                           if FAIL_WORDS.search(t) and t not in snap["texts"]]
            state, problem, warn = classify(
                attr, attr in db_pages, attr in NO_DATA_EXPECTED,
                bool(FAIL_WORDS.search(blob) or FAIL_WORDS.search(chrome)),
                hidden_fail, empties, blob + " " + chrome, before["rows"])
            if problem:
                problems.append(problem)
            if warn:
                warns.append(warn)
            rows.append((attr, title, state, blob[:44]))
    finally:
        if saved:
            _restore(saved)
        try:
            w._skip_close_hooks = True
            w.close()
        except Exception:  # noqa: BLE001
            pass

    print(f"读失败走查 @ {win_w}x{win_h}  根 {root}")
    print(f"{'页面':<16}{'名称':<10}{'状态':<12}现文案")
    for attr, title, state, texts in rows:
        print(f"{attr:<16}{title:<10}{state:<12}{texts}")
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
    print("\n✓ 全部页面通过读失败检查")
    return 0


if __name__ == "__main__":
    sys.exit(main())
