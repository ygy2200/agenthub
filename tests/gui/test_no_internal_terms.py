# -*- coding: utf-8 -*-
"""界面文案不得暴露内部工具名（R197）。

口径（沿用 R171）：主文案说人话，`hub_xxx` 这类工具名只允许出现在 tooltip 里——
用户看界面不需要知道工具签名，agent 需要。此守卫静态扫 `pages/*.py` 的 AST：
凡是把字符串直接送进「可见文案」入口（setText/addItem/setPlaceholderText/
empty_state/各类 Label 构造）且含 `hub_` 的，一律失败，并指出文件与行号。
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL_RE = re.compile(r"hub_[a-z_]{3,}")

VISIBLE_CALLS = {
    "setText", "addItem", "setPlaceholderText", "setHtml", "setWindowTitle",
    "empty_state", "CaptionLabel", "StrongBodyLabel", "BodyLabel", "SubtitleLabel",
    "TitleLabel", "setToolTip_text_forbidden_marker",
}
# 允许出现在可见文案里的例外（人话里确实要提「hub」这个词组的地方）
ALLOW = set()


def _visible_string_args(node: ast.Call):
    name = ""
    if isinstance(node.func, ast.Attribute):
        name = node.func.attr
    elif isinstance(node.func, ast.Name):
        name = node.func.id
    if name not in VISIBLE_CALLS:
        return
    # 剪贴板里的是「给 agent 的提示词」，工具名是内容而非界面文案
    try:
        if "clipboard" in ast.unparse(node.func):
            return
    except Exception:  # noqa: BLE001
        pass
    for a in list(node.args) + [k.value for k in node.keywords]:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            yield a.value
        elif isinstance(a, ast.JoinedStr):
            for v in a.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    yield v.value


def test_no_tool_names_in_visible_copy():
    hits = []
    for f in sorted((ROOT / "pages").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for text in _visible_string_args(node):
                if TOOL_RE.search(text) and text not in ALLOW:
                    hits.append(f"{f.name}:{node.lineno}  {text[:60]}")
    assert not hits, "可见文案里出现内部工具名（应改人话，工具名进 tooltip）：\n" + "\n".join(hits)
