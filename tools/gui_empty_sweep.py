# -*- coding: utf-8 -*-
"""空库三态走查 · 空态横扫（R756，B005 第一步）。

背景：B005「20 个页面的空/错/载入三态逐页走查」连续四批顺延，因为手工逐页截图不可复跑。
R194 的 empty_rhythm_probe 只量**单页单个**空态；本工具一次走完全部页面，对每个页面：
  · 有没有空态（uikit.empty_state 的产物：带 titleLabel 的容器）
  · 空态文案是否为空（标题空 = 用户看到一块无字留白）
  · 空态内部纵向间隔是否被拉伸（> SPACE_XS，R194 的同一把尺子）
  · 页面在 920×600 下是否横向溢出（最小宽度撑破窗口）
  · 每页一张 png 存 output/gui-empty-r756/ 供人眼复核

用法：
    python tools/gui_empty_sweep.py [--root <空目录>] [--out <截图目录>] [--size 920x600]

退出码：0 = 全部通过；1 = 有空态缺陷（拉伸/空标题/页面异常）；2 = 环境不可用。
"""
import os
import sys
from pathlib import Path

import _console  # noqa: E402,F401  控制台 UTF-8 守卫

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))

MAX_GAP = 12  # tokens.SPACE_XS：空态内部相邻元素的许可间隔
# 静态页没有「空态」这个概念，列表为空也不算缺陷（缺了才要报）
NO_EMPTY_EXPECTED = {"help_page", "components_page", "settings_page", "connect_page"}


def _split(argv):
    opts, i = {}, 0
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
        i += 1
    return opts


OPTS = _split(sys.argv[1:])


def _gap_of(box):
    """空态容器内部的最大相邻纵向间隔（与 empty_rhythm_probe 同一口径）。"""
    from PySide6.QtWidgets import QWidget

    rows = []
    for c in box.children():
        if isinstance(c, QWidget) and c.isVisible():
            g = c.geometry()
            rows.append((g.y(), g.height()))
    rows.sort()
    worst, prev_bottom = 0, None
    for y, h in rows:
        if prev_bottom is not None:
            worst = max(worst, y - prev_bottom)
        prev_bottom = y + h
    return worst


def _load_production_font():
    """offscreen 默认字体是宽回退体，截出来的字全是方块，人眼复核等于没有（记忆 #221）。"""
    from PySide6.QtGui import QFontDatabase

    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"):
        if os.path.exists(path):
            QFontDatabase.addApplicationFont(path)
            return path
    return ""


def _visible_texts(page, limit=4):
    """页面上看得见的文案（用于「没有空态但有内容」的区分）。"""
    from PySide6.QtWidgets import QLabel, QPushButton

    out = []
    for cls in (QLabel, QPushButton):
        for c in page.findChildren(cls):
            if not c.isVisible():
                continue
            t = (c.text() or "").strip()
            if t and t not in out:
                out.append(t)
        if len(out) >= limit:
            break
    return out[:limit]


def _list_rows(page) -> int:
    """页面上列表/表格的**真实**条目数。

    「（无错误登记）」这种全角括号占位行不算内容——它正是「列表空却没有标准空态」
    的症状（R756 实测：流水页两条占位行把空列表伪装成有内容）。
    """
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


def _describe_empty(box):
    t = getattr(box, "titleLabel", None)
    d = getattr(box, "descLabel", None)
    b = getattr(box, "actionBtn", None)
    return {
        "title": (t.text() if t is not None else "").strip(),
        "desc": (d.text() if d is not None else "").strip(),
        "action": (b.text() if b is not None else "").strip(),
        "gap": _gap_of(box),
    }


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    size = OPTS.get("--size", "920x600")
    win_w, win_h = (int(x) for x in size.lower().split("x"))
    root = Path(OPTS.get("--root") or ROOT / "_pytest_tmp" / "gui-empty-r756")
    root.mkdir(parents=True, exist_ok=True)
    out = Path(OPTS.get("--out") or ROOT / "output" / "gui-empty-r756")
    out.mkdir(parents=True, exist_ok=True)

    from PySide6.QtWidgets import QApplication, QWidget

    import actions
    import core
    import tokens
    import ui
    from helpers import pump, settle

    app = QApplication.instance() or QApplication([])
    _load_production_font()
    tokens.apply_theme("dark")
    core.get_root = lambda: str(root)
    core.save_config = lambda *a, **k: None   # 走查永不写用户配置（R232 同款）

    w = ui.AgentHubWindow()
    w._skip_close_hooks = True
    problems, rows = [], []
    try:
        w.resize(win_w, win_h)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        for attr, title, *_rest in actions.PAGES:
            page = getattr(w, attr, None)
            if page is None:
                problems.append(f"{attr}：页面不存在")
                rows.append((attr, title, "缺失", 0, 0, ""))
                continue
            try:
                w.switchTo(page)
                settle(app, max_s=3.0)
            except Exception as e:  # noqa: BLE001 单页崩了要继续走完其余页
                problems.append(f"{attr}：切页异常 {type(e).__name__}: {e}")
                rows.append((attr, title, "异常", 0, 0, ""))
                continue
            boxes = [c for c in page.findChildren(QWidget)
                     if hasattr(c, "titleLabel") and c.isVisible()]
            minw = page.minimumSizeHint().width()
            shot = out / f"{attr}.png"
            page.grab().save(str(shot))
            if minw > win_w:
                problems.append(f"{attr}：最小宽度 {minw}px > 窗口 {win_w}px（横向溢出）")
            if not boxes:
                rowsum = _list_rows(page)
                texts = _visible_texts(page)
                if attr in NO_EMPTY_EXPECTED:
                    rows.append((attr, title, "静态页", 0, minw, ""))
                elif rowsum > 0:
                    # 内置内容（如编排中心的流程模板）本来就非空，没有空态是对的
                    rows.append((attr, title, f"有内容·{rowsum} 行", 0, minw,
                                 " | ".join(texts)[:44]))
                else:
                    state = "占位行" if texts else "空白"
                    rows.append((attr, title, state, 0, minw, " | ".join(texts)[:44]))
                    problems.append(f"{attr}：列表为空却没有标准空态"
                                    f"（现显示：{' | '.join(texts)[:60] or '无任何文字'}）")
                continue
            descs = []
            for box in boxes:
                info = _describe_empty(box)
                descs.append(info)
                if not info["title"]:
                    problems.append(f"{attr}：空态标题为空（一块无字留白）")
                if info["gap"] > MAX_GAP:
                    problems.append(f"{attr}：空态内部间隔 {info['gap']}px > {MAX_GAP}px"
                                    f"（{info['title'] or '无标题'}）")
            rows.append((attr, title, f"空态×{len(boxes)}", max(d["gap"] for d in descs),
                         minw, " / ".join(d["title"] for d in descs)[:40]))
    finally:
        try:
            w._skip_close_hooks = True
            w.close()
        except Exception:  # noqa: BLE001
            pass

    print(f"空库走查 @ {win_w}x{win_h}  根 {root}")
    print(f"{'页面':<16}{'名称':<10}{'状态':<10}{'最大间隔':>6}  {'最小宽':>6}  空态标题")
    for attr, title, state, gap, minw, titles in rows:
        print(f"{attr:<16}{title:<10}{state:<10}{gap:>6}  {minw:>6}  {titles}")
    print(f"截图：{out}（{len(rows)} 页）")
    if problems:
        print(f"\n✗ {len(problems)} 处缺陷：")
        for p in problems:
            print("  - " + p)
        return 1
    print("\n✓ 全部页面通过空态检查")
    return 0


if __name__ == "__main__":
    sys.exit(main())
