# -*- coding: utf-8 -*-
"""R232：GUI 测试与诊断工具必须与用户真实配置隔离（静态 + 行为两路守）。

背景：R231 门禁红在 test_status_pages_density（四卡折成 2×2、标题 148px），代码一行
没改。根因是两层泄漏叠加——① hub_root 只 patch 了 get_root/save_config，**读**仍走
用户真实 config.json，用户展开导航后 920 窗只剩 749px 内容区；② gui_snapshot 没设
_skip_close_hooks，w.close() 把测试窗口几何/导航态写回真实配置。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _fn_assignments(path: Path, func: str) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func:
            return [n for n in ast.walk(node) if isinstance(n, (ast.Assign, ast.AnnAssign))]
    return []


def test_snapshot_tool_closes_quietly_and_never_saves_config():
    path = ROOT / "tools" / "gui_snapshot.py"
    assigns = _fn_assignments(path, "capture")
    targets = set()
    for n in assigns:
        tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
        for t in tgts:
            targets.add(ast.unparse(t))
    assert "w._skip_close_hooks" in targets, (
        "gui_snapshot 必须设 w._skip_close_hooks=True——否则 w.close() 会备份/写真实配置")
    assert "core.save_config" in targets, (
        "gui_snapshot 必须把 core.save_config 替换为 no-op——诊断工具不写用户配置")


def test_window_close_has_pytest_fallback():
    """R232 义务（测试/工具忘设 `_skip_close_hooks` 时也不许写用户配置）改钉**行为**。

    R240 把判据抽成 `window._close_hooks_quiet` 之后，本条原来的写法
    （`'os.environ.get("PYTEST_CURRENT_TEST")' in src`）立刻红——它测的是实现形状
    不是义务，函数一抽字面量就搬家。义务本身一条没删：
    ① 这里钉判据行为（含「空值不算 pytest 环境」这种边界）；
    ② 接线由 tests/gui/test_offscreen_close_wiring_r240.py 用真窗口关窗钉——
       它摘掉 `QT_QPA_PLATFORM` 只留 pytest 变量，旧兜底失效时必然红。
    """
    import window

    assert window._close_hooks_quiet({"PYTEST_CURRENT_TEST": "tests/gui/x.py::test_y"},
                                     False) is True, (
        "pytest 环境下窗口关闭必须静默：测试/工具忘设 _skip_close_hooks 时也不许写用户配置")
    assert window._close_hooks_quiet({"PYTEST_CURRENT_TEST": ""}, False) is False, (
        "PYTEST_CURRENT_TEST 是空值时不算测试环境——不能把正常桌面会话误判成静默")


def test_hub_root_fixture_reads_fixed_config(hub_root):
    """布局断言依赖的初始状态必须由测试钉住，而不是跟随用户偏好。"""
    import core
    cfg = core.load_config()
    assert cfg["root"] == hub_root
    assert cfg["nav_collapsed"] is True
    core.save_config({"root": "被污染的根目录"})   # 应被 fixture 换成 no-op
    assert core.load_config()["root"] == hub_root
