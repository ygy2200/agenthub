# -*- coding: utf-8 -*-
"""用户可见文案的「内部术语」审计（R787）。

已有守卫 `tests/gui/test_no_internal_terms.py` 只盯 `hub_xxx` 且只扫 pages/*.py。
实测漏网的三类（空库截图走查 output/gui-baseline/shots）：

  1. 工具名出现在别处：概览页「最近会话（hub_heartbeat 上报 · 近 24 小时）」、
     统计页「agent 可调 hub_health / hub_distill 看完整报告」；
  2. 开发术语当人话：`CLI：tasks --stats`、`journal`、`SQL`、`DB`、`P90`、`WCAG`；
  3. 拼接文案（f-string / 相邻拼接）里藏一半，逐行正则抓不到。

做法与 test_no_internal_terms 同源：AST 取「送进可见文案入口」的字符串（含 f-string
与相邻拼接），再按术语表判定。tooltip / clipboard 是给 agent 或高级用户的，不扫。

用法：python tools/jargon_audit.py [--verbose]
退出码：0=干净；1=有命中。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import _console  # noqa: F401  统一 UTF-8 控制台（R222）

ROOT = Path(__file__).resolve().parent.parent
# 界面文案的落点：页面 + 主窗 + 组件库 + 命令面板（报告是浏览器 HTML，另行审）
FILES = sorted((ROOT / "pages").glob("*.py")) + [
    ROOT / "window.py", ROOT / "uikit.py", ROOT / "palette.py",
]

VISIBLE_CALLS = {
    "setText", "setHtml", "setPlaceholderText", "setWindowTitle",
    "addItem", "insertItem", "addItems", "setTitle", "setSubtitle",
    "empty_state", "section_head", "_page_head",
    "CaptionLabel", "BodyLabel", "StrongBodyLabel", "SubtitleLabel", "TitleLabel",
    "PushButton", "PrimaryPushButton", "TransparentPushButton", "CheckBox",
    "RadioButton", "SwitchButton", "MessageBox", "InfoBar",
    "warning", "info", "success", "error", "showMessage",
}
# 这些参数是「给 agent 的提示词/文件名/工具名说明」，不是屏幕上的人话
SKIP_KWARGS = {"parent", "userData", "key", "icon", "toolTip"}
SKIP_METHODS = {"setToolTip", "setStatusTip", "setWhatsThis"}

# (术语, 说明, 允许出现的文件 glob)
TERMS = [
    (re.compile(r"hub_[a-z_]{3,}"), "内部工具名（应改人话，工具名进 tooltip）", ()),
    (re.compile(r"\bCLI\b"), "缩写 CLI（应说「命令行」）", ()),
    (re.compile(r"\b(?:SQL|SQLite|DB)\b"), "数据库实现名（应说「数据库」）", ()),
    (re.compile(r"\bjournal\b"), "内部表名 journal（应说「流水」）", ()),
    (re.compile(r"\bjsonl\b"), "文件格式名（应说「导出文件」）", ()),
    (re.compile(r"\bP90\b"), "统计分位缩写（应说「慢 10% 耗时」并进 tooltip）", ()),
    (re.compile(r"\bWCAG\b"), "无障碍标准编号（应说「对比度验算」）", ()),
    (re.compile(r"虚拟滚动|FnWorker|QThread"), "实现术语（应说用户能感知的行为）", ()),
    (re.compile(r"\btoken\b", re.I), "技术词 token（应说「凭据」）", ()),
    (re.compile(r"\bMCP\b"), "协议缩写——只有接入/帮助/资产页需要原词",
     ("connect_page.py", "help_page.py", "hub_page.py")),
]


def _walk_strings(node: ast.AST):
    """把 f-string 的常量段与相邻拼接都还原成可读文本。"""
    for a in ast.walk(node):
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            yield a.value


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _visible_strings(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in SKIP_METHODS or name not in VISIBLE_CALLS:
            continue
        # 剪贴板里装的是「给 agent 的提示词」：工具名是内容，不是屏幕文案
        try:
            if "clipboard" in ast.unparse(node.func):
                continue
        except Exception:  # noqa: BLE001
            pass
        # seg.addItem(key, 显示名)：第一个参数是路由键，不给人看
        args = list(node.args)
        if name == "addItem" and len(args) >= 2 and isinstance(args[0], ast.Constant)                 and isinstance(args[0].value, str) and args[0].value.isascii():
            args = args[1:]
        for a in args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                yield node.lineno, a.value
            elif isinstance(a, ast.JoinedStr):
                txt = "".join(v.value for v in a.values
                              if isinstance(v, ast.Constant) and isinstance(v.value, str))
                if txt:
                    yield node.lineno, txt
        for kw in node.keywords:
            if kw.arg in SKIP_KWARGS:
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                yield node.lineno, kw.value.value


def audit() -> list[str]:
    hits = []
    for f in FILES:
        if not f.exists():
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for lineno, text in _visible_strings(tree):
            # 剪贴板/提示词常量不算界面文案
            if "python cli.py" in text or "hub_" in text and "对应工具" in text:
                continue
            for rx, why, allow in TERMS:
                if allow and f.name in allow:
                    continue
                m = rx.search(text)
                if m:
                    hits.append(f"{f.name}:{lineno}  [{m.group(0)}] {why} :: {text[:80]}")
    return hits


def main() -> int:
    hits = audit()
    if not hits:
        print("内部术语审计：0 处（界面文案只说人话）")
        return 0
    print(f"内部术语审计：{len(hits)} 处")
    seen = set()
    for h in hits:
        if h in seen:
            continue
        seen.add(h)
        print("  " + h)
    return 1


if __name__ == "__main__":
    sys.exit(main())
