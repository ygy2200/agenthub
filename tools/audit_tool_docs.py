# -*- coding: utf-8 -*-
"""工具示例 × schema 漂移审计（R202）。

扫描三处「人照着抄」的示例：
  1. TOOLS 里每个工具自己的 description（agent 直接看到的调用范例）
  2. README.md 的当前用法段（跳过 `| v...` 版本演进表）
  3. docs/运维手册.md

检查每条 `hub_xxx(key=...)` 示例：工具名是否存在、参数名是否在
inputSchema.properties 里、必填参数是否齐全。另查 schema 自身的一致性与
description 里提到但不存在/拼错的工具名。

用法：python tools/audit_tool_docs.py [--verbose]
退出码：0=无漂移；1=有问题。
"""
import re
import sys
from pathlib import Path

import _console  # noqa: E402,F401  控制台 UTF-8 守卫

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agenthub_mcp as m  # noqa: E402

CALL_RE = re.compile(r"\b(hub_[a-z][a-z0-9_]*)\(([^()]*)\)")
ARG_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=|^([A-Za-z_][A-Za-z0-9_]*)$")
NAME_RE = re.compile(r"\b(hub_[a-z][a-z0-9_]+)\b")


def kwargs(text: str) -> tuple:
    """(named, positional)：key=value 与文档里的位置写法 (task_id, agent)。

    位置写法是文档简写（真实 MCP 调用必须带参数名），只用于「必填是否出现」，
    不参与「参数名不存在」判定。
    """
    named, pos = [], []
    for tok in re.split(r",(?![^\[]*\])", text):
        tok = tok.strip()
        if not tok or tok.strip("….") == "":
            continue
        m = ARG_RE.match(tok)
        if m:
            name = m.group(1) or m.group(2)
            bucket = named if m.group(1) else pos
            if name not in bucket:
                bucket.append(name)
    return named, pos


def audit_source(label: str, text: str, tools: dict, issues: list, lineno=None):
    for cm in CALL_RE.finditer(text):
        name, argtext = cm.group(1), cm.group(2)
        where = f"{label}:{lineno}" if lineno else label
        t = tools.get(name)
        if t is None:
            issues.append(f"{where} 示例里的工具不存在：{name}")
            continue
        props = (t.get("inputSchema") or {}).get("properties") or {}
        req = (t.get("inputSchema") or {}).get("required") or []
        named, pos = kwargs(argtext)
        got = named + pos
        for k in named:
            if k not in props:
                issues.append(f"{where} {name} 示例参数 {k}= 不在 schema 里"
                              f"（可用：{', '.join(list(props)[:8]) or '无'}）")
        missing = [r for r in req if r not in got]
        # 允许示例只演示某一个分支：出现 ... 或「等」视为节选
        if missing and "..." not in argtext and "…" not in argtext:
            issues.append(f"{where} {name} 示例缺必填参数：{', '.join(missing)}")


def collect_issues() -> list:
    tools = {t["name"]: t for t in m.TOOLS}
    issues: list = []

    for t in m.TOOLS:
        name = t["name"]
        desc = (t.get("description") or "").strip()
        sch = t.get("inputSchema") or {}
        props = sch.get("properties") or {}
        req = sch.get("required") or []
        if not desc:
            issues.append(f"工具 {name} 没有 description")
        if sch.get("type") != "object":
            issues.append(f"工具 {name} 的 inputSchema.type 不是 object")
        for r in req:
            if r not in props:
                issues.append(f"工具 {name} 的必填 {r} 不在 properties 里")
        for k, v in props.items():
            if not isinstance(v, dict) or not v.get("type"):
                issues.append(f"工具 {name} 的参数 {k} 缺 type")
            elif not str(v.get("description") or "").strip():
                issues.append(f"工具 {name} 的参数 {k} 缺 description")
        audit_source(f"工具 {name} 的 description", desc, tools, issues)
        # 描述里提到但不存在的工具名（防改名后残留）
        for nm in set(NAME_RE.findall(desc)):
            if nm not in tools:
                issues.append(f"工具 {name} 的 description 提到不存在的工具：{nm}")

    for path in (ROOT / "README.md", ROOT / "docs" / "运维手册.md"):
        if not path.is_file():
            continue
        # 只按 \n 计行：splitlines() 会把 \u2028/\x0b 等也算一行，报错行号会漂
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
            if line.lstrip().startswith("| v"):
                continue  # 版本演进表是历史记录，不改写
            audit_source(path.name, line, tools, issues, lineno=i)

    # 去重但保留顺序
    seen, uniq = set(), []
    for it in issues:
        if it not in seen:
            seen.add(it)
            uniq.append(it)
    return uniq


def main() -> int:
    verbose = "--verbose" in sys.argv
    uniq = collect_issues()
    print(f"工具示例审计：{len(m.TOOLS)} 个工具，{len(uniq)} 处漂移")
    for it in uniq[:60]:
        print("  - " + it)
    if len(uniq) > 60:
        print(f"  …（还有 {len(uniq) - 60} 处，--verbose 不截断）" if not verbose else "")
    return 1 if uniq else 0


if __name__ == "__main__":
    sys.exit(main())
