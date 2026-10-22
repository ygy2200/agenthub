# -*- coding: utf-8 -*-
"""D7 工具：GUI **结构快照**基线——比像素黄金图更适合本机约束的视觉回归守卫。

为什么不是像素「黄金图对比」：
  视觉回归的标准做法是存一张基准 png 然后逐像素比。但本机刚吃过这个亏（R8）：
  offscreen 下 Qt 把应用默认字体解析成宽拉丁回退体（'Claude Code' 132px），
  生产（Microsoft YaHei UI）只要 73px——**同一份代码在两个环境截出的图天然不同**，
  像素对比会变成误报工厂。本脚本因此改为对比**结构**：

    每页快照 = {控件类名计数, 关键静态文本集合, 最小高度}

  结构对字体度量/DPI 的敏感度远低于像素，却仍能抓住真实的布局与内容回归
  （区块消失 / 文案被改坏 / 控件数骤降）。

  png 仍然存（output/gui-baseline/shots/*.png），但定位是**给人看的参考图**，
  不做自动比对——自动比对只认结构快照。

  固定项（否则不可比）：字体（显式加载生产字体）、主题、窗口尺寸、临时库+固定
  种子数据。动态内容（日期/时间/路径/id/计数/体积）采集时被归一化，否则每次都
  不一样、基线就没有意义。

用法：
    python tools/gui_snapshot.py              # 比对（无基线时自动创建）
    python tools/gui_snapshot.py --update     # 强制重写基线
    python tools/gui_snapshot.py --shots      # 同时导出参考图
    python tools/gui_snapshot.py --json       # 机器可读输出
退出码：0=一致（或已创建基线）；1=发现回归；2=环境不可用
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "gui"))

# 基线**必须入库**（放 tests/ 下）：它是测试资产，跟着代码一起版本化。
# 曾经放在 output/ 下，但 output/ 被 .gitignore 忽略——那样别人 clone 下来没有基线，
# 守卫就走 skip 分支，等于静默失效（守卫不响比守卫误报危险得多）。
# 参考图 png 反而是**产物**，放在被忽略的 output/ 下正合适。
BASELINE = ROOT / "tests" / "gui" / "baseline"
SNAP = BASELINE / "snapshot.json"
SHOTS = ROOT / "output" / "gui-baseline" / "shots"

WIN_W, WIN_H = 1280, 820          # 固定尺寸：几何可比的前提
THEME = "dark"

# 动态内容归一化：凡是「每次跑都可能变」的文本，一律打码后再入快照
_DYNAMIC = (
    # 总览问候语随时间变化（上午/下午/晚上好）：不归一化的话，基线在 18:00 后必红
    (re.compile(r"(?:早上好|上午好|中午好|下午好|晚上好|你好)"), "<GREETING>"),
    (re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"), "<DATE>"),
    (re.compile(r"\d{1,2}:\d{2}(:\d{2})?"), "<TIME>"),
    (re.compile(r"[A-Za-z]:[\\/][^\s\"']*"), "<PATH>"),
    (re.compile(r"#\d+"), "<ID>"),
    (re.compile(r"\d+(\.\d+)?\s?(KB|MB|GB)"), "<SIZE>"),
    (re.compile(r"\d+\s*条"), "<N> 条"),
    (re.compile(r"\d+\s*个"), "<N> 个"),
    (re.compile(r"\d+\s*次"), "<N> 次"),
    # agent 概览行的技能/MCP/记忆数是本机安装状态的投影，不是 UI 结构；
    # 装一个技能就变一次会让结构基线天天误报（R9 实测 165→186 即触发）。
    (re.compile(r"技能\s*\d+"), "技能 <N>"),
    (re.compile(r"MCP\s*\d+"), "MCP <N>"),
    (re.compile(r"记忆\s*\d+"), "记忆 <N>"),
    # 接入页状态行（R33 去部署化后）：真实反映 ready/stale/missing/no-python 四态，
    # 陈旧态还会列出当时漂移的文件名——整句都是运行态投影而非 UI 结构，整句打码。
    (re.compile(r"^(?:⚠\s*)?(?:未找到可用的 python\.exe|MCP server|服务端就绪).*"),
     "<DEPLOY_STATUS>"),
)


def _norm(text: str) -> str:
    text = (text or "").strip()
    for pat, rep in _DYNAMIC:
        text = pat.sub(rep, text)
    return text[:120]


def _seed(root: str) -> None:
    """固定种子数据：空库反映不出真实布局，真库又满是动态内容——用少量可复现的数据。"""
    import brain
    brain.init_db(root)
    for i in range(3):
        brain.add_record(root, f"种子项目-{i % 2}", "zcode", "2026-01-0%d" % (i + 1),
                         f"种子记录标题 {i}",
                         "【目的】基线\n【做了什么】固定种子数据\n"
                         "【验证结果】可复现\n【如何回滚】删")
    brain.add_memory(root, "种子记忆：代理端口是 7890", kind="fact", agent="zcode",
                     project="种子项目-0")
    brain.add_memory(root, "种子踩坑：端口冲突时先查占用", kind="lesson", agent="workbuddy",
                     project="种子项目-1")


def _load_production_font(app) -> str:
    """显式加载生产字体（记忆 #221）：offscreen 默认字体是宽回退体，会让布局失真。"""
    from PySide6.QtGui import QFontDatabase
    for path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"):
        if os.path.exists(path):
            QFontDatabase.addApplicationFont(path)
            return path
    return ""


def capture(update_shots: bool = False) -> dict:
    """启动离屏窗口，遍历全部页面采集结构快照（约 20s）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import tempfile

    from PySide6.QtWidgets import QApplication, QWidget

    tmp = Path(tempfile.mkdtemp(prefix="gui_snap_"))
    (tmp / "hub").mkdir(parents=True)
    root = str(tmp / "hub")

    app = QApplication.instance() or QApplication([])
    font_src = _load_production_font(app)
    if not font_src:
        sys.stderr.write("未找到生产字体（msyh），布局度量会偏离生产\n")

    _seed(root)

    import actions
    import core
    import tokens
    import ui
    from helpers import pump, settle

    # 与 tests/gui/conftest 同法指向临时库。**必须 try/finally 还原**：core.get_root 是
    # 模块级全局，在 pytest 进程内改了不还原会污染后续所有测试（只跑这一个用例时看不出来，
    # 一加新用例就开始互相干扰——这类 bug 极难定位）。
    _orig_get_root = core.get_root
    core.get_root = lambda: root
    tokens.apply_theme(THEME)

    # agent 探测扫的是本机真实目录，结果随安装状态漂移（技能/MCP 数量变化就会改文案）。
    # 结构基线必须只反映 UI，故探测固定为两个种子 agent；异步线程仍照常走完。
    import agentscore
    _orig_detect = agentscore.detect_agents
    _seed_agents = [
        agentscore.AgentInfo(name="Seed Agent A", home="~/.seed-a", detected=True),
        agentscore.AgentInfo(name="Seed Agent B", home="~/.seed-b", detected=True),
    ]
    agentscore.detect_agents = lambda extra=None: _seed_agents

    out = {"_meta": {"theme": THEME, "size": [WIN_W, WIN_H], "font": font_src}, "pages": {}}
    w = ui.AgentHubWindow()
    try:
        w.resize(WIN_W, WIN_H)
        w.show()
        pump(app, lambda: getattr(w.project_page, "snap", None) is not None, 30)
        # 总览的 agent 行来自 300ms 定时器 + 后台探测：不等它就会采到半空页面，
        # 表现为时有时无的「文案消失」（R10 实测：新增 3 个 agent 后探测变慢即触发）。
        pump(app, lambda: bool(getattr(w.hub_page, "agents", None)), 8.0)

        for attr, title, *_rest in actions.PAGES:
            page = getattr(w, attr, None)
            if page is None:
                out["pages"][attr] = {"error": "页面不存在"}
                continue
            w.switchTo(page)
            # 固定 120ms 在机器负载高时采到半加载页面（playbook/signal 的 agent 下拉
            # 文案时有时无）——改为等事件队列真正安静，最多 1.5s。
            settle(app, max_s=1.5)
            widgets = page.findChildren(QWidget)
            texts = []
            for wd in widgets:
                try:
                    t = wd.text() or ""
                except Exception:      # noqa: BLE001  某些自定义控件的 text() 需要参数
                    t = ""
                t = _norm(t)
                if t and len(t) > 1:
                    texts.append(t)
            out["pages"][attr] = {
                "title": title,
                "widget_count": len(widgets),
                "class_count": dict(Counter(type(wd).__name__ for wd in widgets)),
            # **不要截断**：曾经用 `sorted(...)[:60]` 只留前 60 条，结果 z 开头的文案
            # （如种子数据里的 agent 名 'zcode'）恰好排在最后——页面文案数在 60 附近
            # 正常波动时，它就时有时无，守卫开始报假「文案消失」。（2026-10-19 实测：
            # 连采两次都有 zcode，但比对报消失，就是因为基线那次刚好没被截断掉。）
            # **排序后截断 = 让截断边界落在字母序上**，而字母序与「内容是否稳定」毫无
            # 关系；要么不截断，要么按稳定性排序。这里文案量本就不大，直接不截断。
            "texts": sorted(set(texts)),
                "min_h": min([wd.minimumHeight() for wd in widgets] or [0]),
            }
            if update_shots:
                SHOTS.mkdir(parents=True, exist_ok=True)
                pm = page.grab()
                if not pm.isNull():
                    pm.save(str(SHOTS / f"{attr}.png"))
    finally:
        core.get_root = _orig_get_root
        agentscore.detect_agents = _orig_detect
        w.close()
    return out


def diff(before: dict, after: dict) -> list:
    """结构差异：只报**值得人看**的变化（文案消失/区块缺失/控件骤变），忽略计数微动。"""
    problems = []
    bp, ap = before.get("pages", {}), after.get("pages", {})
    for attr in sorted(set(bp) | set(ap)):
        b, a = bp.get(attr), ap.get(attr)
        if b is None or a is None:
            problems.append(f"{attr}：页面在基线与当前之间消失/新增")
            continue
        if "error" in a and "error" not in b:
            problems.append(f"{attr}：{a['error']}")
            continue
        lost = set(b.get("texts", [])) - set(a.get("texts", []))
        if lost:
            problems.append(f"{attr}（{a.get('title', '')}）：文案消失 {sorted(lost)[:5]}")
        bc, ac = b.get("widget_count", 0), a.get("widget_count", 0)
        if bc and (ac < bc * 0.6 or ac > bc * 1.8):
            problems.append(f"{attr}：控件数异常变化 {bc}→{ac}（±40% 阈值）")
    return problems


def main() -> int:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("错误：未安装 PySide6，GUI 快照不可用")
        return 2
    if sys.platform != "win32":
        print("非 Windows：生产字体与环境不同，快照仅供参考")

    update = "--update" in sys.argv
    shots = "--shots" in sys.argv or update
    current = capture(update_shots=shots)

    if update or not SNAP.exists():
        BASELINE.mkdir(parents=True, exist_ok=True)
        SNAP.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入基线：{SNAP}（{len(current['pages'])} 页）")
        if shots:
            print(f"  参考图：{SHOTS}")
        return 0

    baseline = json.loads(SNAP.read_text(encoding="utf-8"))
    if baseline.get("_meta", {}).get("size") != current["_meta"]["size"]:
        print(f"基线尺寸 {baseline['_meta']['size']} 与当前 {current['_meta']['size']} 不同"
              "——几何不可比，请 --update 重建基线")
        return 1
    problems = diff(baseline, current)
    if "--json" in sys.argv:
        print(json.dumps({"pages": len(current["pages"]), "problems": problems},
                         ensure_ascii=False, indent=2))
    if problems:
        print(f"GUI 结构回归：{len(problems)} 处")
        for p in problems:
            print("   -", p)
        print("\n（如为有意改动，跑 python tools/gui_snapshot.py --update 重建基线）")
        return 1
    print(f"GUI 结构快照一致（{len(current['pages'])} 页，比对 {SNAP.name}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
