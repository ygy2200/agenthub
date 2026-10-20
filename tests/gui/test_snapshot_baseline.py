# -*- coding: utf-8 -*-
"""gui 层：D7 **结构快照**回归守卫（防视觉/布局无声变化）。

为什么用结构而不是像素黄金图：本机 R8 刚被字体伪象咬过——offscreen 下 Qt 把应用
默认字体解析成宽拉丁回退体（'Claude Code' 132px），生产（Microsoft YaHei UI）只要
73px，同一份代码在两个环境截出的图天然不同，逐像素比必然误报。改比结构：
控件类名计数 + 去重静态文案 + 最小高度，对字体/DPI 敏感度低得多，但区块消失、
文案被改坏、控件数骤降照样抓得住。

两条用例：
  ① 有基线 → **子进程**采集当前快照并比对，零差异才算过；无基线则 skip（不静默
     放行：skip 原因写明如何生成基线）。必须子进程：与基线生成同处一个全新解释器，
     否则会读到前序 GUI 用例留下的全局状态，全序列跑报假差异（R10 实测「WorkBuddy
     文案消失」，单跑却通过）。
  ② **反向验证 diff 逻辑**：往基线副本里注入三种回归（删文案 / 控件数骤变 /
     页面消失），守卫必须全部报出；完全一致时必须零问题。守卫自己失灵 = 静默失效，
     这条是本套件的惯例。

耗时：① 约 20s（启动离屏窗口遍历 18 页），② 毫秒级。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def test_gui_structural_snapshot_matches_baseline():
    import pytest
    try:
        import tools.gui_snapshot as g
    except ImportError as e:
        pytest.skip(f"GUI 快照工具不可用（{e}）——GUI 侧检查，可跳过")

    if not g.SNAP.exists():
        pytest.skip("尚无 GUI 结构基线——先跑一次 "
                    "`python tools/gui_snapshot.py --update` 生成后再回来守卫")

    baseline = json.loads(g.SNAP.read_text(encoding="utf-8"))
    assert len(baseline.get("pages", {})) >= 10, (
        f"基线本身页面过少：{len(baseline.get('pages', {}))}——疑似基线损坏，"
        "跑 python tools/gui_snapshot.py --update 重建")

    # 子进程采集：环境与 --update 生成基线时完全一致，且不受本进程前面用例的
    # 全局状态（core.get_root / 主题 / 已实例化窗口）污染。
    import os
    import subprocess
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gui_snapshot.py"), "--json"],
        cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300)
    detail = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        "GUI 结构回归（如为有意改动，跑 python tools/gui_snapshot.py --update 重建基线）：\n"
        + detail)


def test_snapshot_diff_catches_regressions():
    """反向验证：守卫必须真能抓到回归，且完全一致时不误报。"""
    import pytest
    try:
        import tools.gui_snapshot as g
    except ImportError as e:
        pytest.skip(f"GUI 快照工具不可用（{e}）")
    if not g.SNAP.exists():
        pytest.skip("无基线，diff 反向验证一并跳过")

    base = json.loads(g.SNAP.read_text(encoding="utf-8"))
    cur = json.loads(g.SNAP.read_text(encoding="utf-8"))

    # 完全一致 → 零问题（不误报）
    assert g.diff(base, cur) == [], "相同的两份快照仍报了差异（误报）"

    # ① 基线有一条文案，当前没有 → 必须报「文案消失」
    b1 = json.loads(json.dumps(base, ensure_ascii=False))
    page = next(iter(b1["pages"]))
    b1["pages"][page]["texts"].append("【这条基线文案被删了】")
    assert any("文案消失" in p for p in g.diff(b1, cur)), "守卫失灵：文案消失未被报出"

    # ② 控件数骤变（超出 ±40%）→ 必须报
    b2 = json.loads(json.dumps(base, ensure_ascii=False))
    b2["pages"][page]["widget_count"] = 100000
    assert any("控件数异常" in p for p in g.diff(b2, cur)), "守卫失灵：控件数骤变未被报出"

    # ③ 基线有而当前没有的页面 → 必须报
    b3 = json.loads(json.dumps(base, ensure_ascii=False))
    del b3["pages"][page]
    assert any(page in p for p in g.diff(b3, cur)), "守卫失灵：页面消失未被报出"
