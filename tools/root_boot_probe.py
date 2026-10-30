# -*- coding: utf-8 -*-
"""首启/根目录失效走查（R771）：用户第一眼看到什么、有没有下一步。

背景：R762/R766 覆盖的是「有库但读失败」；从没测过**根本没有可用根目录**的三种开机态：
  · unset —— 全新机器，没有配置文件（root 为空）
  · dead  —— 配置里的根目录所在盘/目录不存在（移动硬盘拔了、盘符变了）
  · empty —— 目录在但里面什么都没有（第一次指过去）

判据（用户视角）：
  · 三种状态下，首屏都必须给出**可执行的下一步**（设置/选择/初始化根目录类文案），
    且不能是空白页；
  · dead 必须出现「根目录不存在」并且带恢复指引，不能假装库是空的；
    设置页（用户动手修的地方）还要能看到「目录不可用」状态行（R774）；
  · 任何子进程崩溃/超时记为缺陷。

用法：
    python tools/root_boot_probe.py [--out <目录>]

退出码：0 = 三种状态都有可执行指引；1 = 有缺陷；2 = 环境不可用。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import _console  # noqa: E402,F401  控制台 UTF-8 守卫

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GUIDE = ("设置", "根目录", "选择目录", "初始化", "选一个目录")


def _dead_root() -> str:
    """确定不存在的路径：优先落在不存在的盘符上（真实「盘不在」），
    候选盘都存在时退回临时目录下不存在的子目录——判定分支相同，不因机器盘符变化失真。"""
    suffix = f"__agenthub_dead_{uuid.uuid4().hex[:8]}"
    for letter in "ZYXQRT":
        if not os.path.exists(f"{letter}:/"):
            return f"{letter}:/{suffix}"
    return str(Path(tempfile.gettempdir()) / suffix)


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


def _visible_texts(widget):
    from PySide6.QtWidgets import QLabel, QPushButton

    out = []
    for cls in (QLabel, QPushButton):
        for c in widget.findChildren(cls):
            if not c.isVisible():
                continue
            t = (c.text() or "").strip()
            if t and t not in out:
                out.append(t)
    return out


def _child(mode: str, out_dir: Path) -> int:
    """子进程：真实窗口 + 假 HOME，观察首屏。结果写 out_dir/<mode>.json。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT / "tests" / "gui"))
    from PySide6.QtWidgets import QApplication

    import core
    import tokens
    import ui
    from helpers import pump, settle

    app = QApplication.instance() or QApplication([])
    _load_production_font()
    tokens.apply_theme("dark")

    rec = {"mode": mode, "root": core.get_root(), "ok": True, "error": "",
           "landing": "", "texts": [], "chrome": [], "settings_texts": [],
           "screenshot": "", "config_dir": str(core.CONFIG_DIR)}
    w = None
    try:
        w = ui.AgentHubWindow()
        w._skip_close_hooks = True
        w.resize(1100, 760)
        w.show()
        pump(app, lambda: w._apply_count >= 1, 10)
        settle(app, max_s=3.0)
        rec["applies"] = w._apply_count
        landing = w.stackedWidget.currentWidget()
        rec["landing"] = w._page_attr(landing) or type(landing).__name__
        rec["texts"] = _visible_texts(landing)[:14]
        rec["chrome"] = [t for t in _visible_texts(w) if t not in rec["texts"]][:14]
        shot = out_dir / f"{mode}.png"
        try:
            landing.grab().save(str(shot))   # 先截图：切到设置页后落地页已隐藏
            rec["screenshot"] = str(shot)
        except Exception:  # noqa: BLE001
            pass
        # R774：设置页才是用户动手修目录的地方，状态行必须能读出病因
        try:
            w.switchTo(w.settings_page)
            app.processEvents()
            rec["settings_texts"] = _visible_texts(w.settings_page)[:14]
        except Exception:  # noqa: BLE001  设置页读不出来只影响这条附加判据
            rec["settings_texts"] = []
    except Exception as e:  # noqa: BLE001
        rec["ok"] = False
        rec["error"] = f"{type(e).__name__}: {e}"
    finally:
        if w is not None:
            try:
                w._skip_close_hooks = True
                w.close()
            except Exception:  # noqa: BLE001
                pass
    (out_dir / f"{mode}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if rec["ok"] else 1


def _write_home_config(home: Path, mode: str, empty_root: Path) -> None:
    cfg_dir = home / ".agenthub"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    if mode == "unset":
        return  # 全新机器：连配置文件都没有
    root = _dead_root() if mode == "dead" else str(empty_root)
    (cfg_dir / "config.json").write_text(
        json.dumps({"root": root, "theme_mode": "dark"}, ensure_ascii=False),
        encoding="utf-8")


def main() -> int:
    if "--child" in sys.argv:
        # 布尔旗标不能用 _split（它会把紧随其后的 --mode 当成旗标的值吃掉）
        def _val(flag, default=""):
            return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default
        mode = _val("--mode")
        out_dir = Path(_val("--out") or (ROOT / "output" / "root-boot-r771"))
        out_dir.mkdir(parents=True, exist_ok=True)
        return _child(mode, out_dir)

    out = Path(OPTS.get("--out") or (ROOT / "output" / "root-boot-r771"))
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="agenthub-rootboot-"))
    problems, rows = [], []
    t0 = time.time()
    try:
        for mode in ("unset", "dead", "empty"):
            home = tmp / f"home-{mode}"
            empty_root = tmp / f"empty-root-{mode}"
            empty_root.mkdir(parents=True, exist_ok=True)
            _write_home_config(home, mode, empty_root)
            env = dict(os.environ)
            env.update({
                "HOME": str(home),
                "USERPROFILE": str(home),
                "HOMEDRIVE": home.drive or "C:",
                "HOMEPATH": str(home)[len(home.drive):] if home.drive else str(home),
                "PYTHONPATH": str(ROOT),
                "PYTHONUTF8": "1",
                "QT_QPA_PLATFORM": "offscreen",
            })
            cp = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--child",
                 "--mode", mode, "--out", str(out)],
                cwd=str(ROOT), env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120)
            rec_path = out / f"{mode}.json"
            if cp.returncode != 0 or not rec_path.exists():
                tail = (cp.stderr or cp.stdout or "").strip().splitlines()[-3:]
                problems.append(f"{mode}：子进程失败 rc={cp.returncode} {' | '.join(tail)}")
                rows.append((mode, "?", 0, "", "✗"))
                continue
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
            blob = " ".join(rec["texts"] + rec["chrome"])
            stext = " ".join(rec.get("settings_texts", []))
            has_guide = any(g in blob for g in GUIDE)
            has_dead_hint = "根目录不存在" in blob
            applies = rec.get("applies", 0)
            if not rec["ok"]:
                problems.append(f"{mode}：窗口启动异常 {rec['error']}")
            elif not (rec["texts"] or rec["chrome"]):
                problems.append(f"{mode}：首屏完全空白，没有任何文案")
            elif mode == "unset" and not (rec["landing"] == "settings_page" or has_guide):
                problems.append(f"unset：没有根目录却不给设置入口"
                                f"（落在 {rec['landing']}，看到：{blob[:60] or '无'}）")
            elif mode == "dead" and not has_dead_hint:
                problems.append(f"dead：根目录不存在却不报错，看起来只是空库"
                                f"（看到：{blob[:70]}）")
            elif mode == "dead" and "目录不可用" not in stext:
                problems.append(f"dead：设置页没有标出「目录不可用」"
                                f"（看到：{stext[:60] or '无'}）")
            elif mode == "empty" and applies < 1:
                problems.append("empty：空目录始终没有完成一次扫描/应用（页面永远等不到数据）")
            ok = not any(p.startswith(mode + "：") for p in problems)
            rows.append((mode, rec["root"][:34] or "(空)", applies,
                         blob[:78], "✓" if ok else "✗"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"首启/根目录失效走查  输出 {out}  用时 {time.time() - t0:.1f}s")
    print(f"{'状态':<8}{'root':<36}{'刷新':>4}  首屏文案（截断）")
    for mode, root, applies, blob, mark in rows:
        print(f"{mode:<8}{root:<36}{applies:>4}  {mark} {blob}")
    print(f"截图：{out}（3 张）")
    if problems:
        print(f"\n✗ {len(problems)} 处缺陷：")
        for p in problems:
            print("  - " + p)
        return 1
    print("\n✓ unset 落设置页 / dead 报「根目录不存在」且设置页标「目录不可用」 / empty 完成扫描")
    return 0


if __name__ == "__main__":
    sys.exit(main())
