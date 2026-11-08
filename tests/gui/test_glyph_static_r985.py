# -*- coding: utf-8 -*-
"""R985：字形守卫从「运行时扫可见控件」补成「静态扫全部字符串」——堵住数据依赖盲区。

为什么要有这一层：R982 我在帮助页写了 `⚠已被 #N 修正`，运行时用例当场红——但同一轮
我在桌面列表徽标里也用了 `⚠`，**那条用例一直没红**。原因不是它没问题，而是
`test_glyph_coverage` 跑的是种子库，种子库没有「被修正」的记忆，徽标根本不渲染。
生产库 18 条带标注，真机上那些行就是方框。

同样的盲区用静态扫描一扫就现形（本次实测另有 3 处）：
  · `settings_page._patrol_text` 的 `✓ / ✗`（只有点过「自检」才出现）
  · `today_page` 优先级菜单的 `✓`（只有右键才出现）

所以这层守卫不依赖任何运行时数据：**只要源码里的字符串常量出现禁用符号就红**。
边界：注释与文档字符串不算（用户看不见），f-string 的字面段算（会显示）。
"""

import ast
import pathlib

FORBIDDEN = set("✓✗⚠✔✘ⓘ▶◀⏸⏵")

ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGETS = sorted(
    [p for p in ROOT.glob("pages/*.py")]
    + [ROOT / n for n in ("uikit.py", "window.py", "actions.py", "tokens.py")]
)


def _docstring_nodes(tree):
    """收集所有 docstring 的节点 id，用来排除（注释本身不是常量，自动排除）。"""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _bad_strings(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    skip = _docstring_nodes(tree)
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in skip:
            continue
        hits = sorted({ch for ch in node.value if ch in FORBIDDEN})
        if hits:
            out.append((node.lineno, hits, node.value.strip()[:60]))
    return out


def test_no_forbidden_glyph_in_gui_source():
    """GUI 源码里任何会显示给用户的字符串都不许用禁用符号。"""
    problems = []
    for path in TARGETS:
        if not path.exists():
            continue
        for lineno, hits, text in _bad_strings(path):
            problems.append(
                f"{path.relative_to(ROOT)}:{lineno} {'/'.join(hits)} → {text}")
    assert not problems, (
        "GUI 源码里有字体没字形的符号（真机显示方框）：\n" + "\n".join(problems[:12])
        + "\n替换建议见 tests/gui/test_glyph_coverage.SAFE_REPLACEMENTS")


def test_scanner_actually_sees_something(qapp, tmp_path):
    """反证：扫描器不是永远绿的摆设——喂一个带 ⚠ 的临时文件必须报出来。

    首版把「文档」写成模块级赋值 `Y = \"\"\"…\"\"\"`，结果被正确报出（那是变量值，
    用户可能看见）。真正的 docstring 必须是函数/类/模块的**首个语句**，改用函数包住。
    """
    f = tmp_path / "fake_page.py"
    f.write_text(
        'X = "⚠ 注意"\n'
        'def g():\n'
        '    """文档里写 ⚠ 不算（用户看不见）"""\n'
        '    return 1\n', encoding="utf-8")
    rows = _bad_strings(f)
    assert rows, "扫描器漏掉了字符串常量里的禁用符号"
    assert rows[0][1] == ["⚠"], rows
    assert len(rows) == 1, f"docstring 不该被算进来：{rows}"
