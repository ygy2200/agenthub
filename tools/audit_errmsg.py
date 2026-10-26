# -*- coding: utf-8 -*-
"""错误文案可行动性审计（R115 建，R180 改 AST 提取）。

旧版按行正则抓字面量，遇到相邻字符串拼接只取第一段：动作写在第二段时被误判
「无动作词」（R179 实测 46 条里 32 条候选，多数是假阳性）。新版用 AST 还原
Return/Raise 的完整文案（含 f-string 与相邻拼接），再分三类：
  · 有动作：出现动词/工具名/允许值/示例；
  · 纯透传：整条只是 `错误：{err}`，动作在内层错误里，不计候选；
  · 候选：既没有动作也不是纯透传，需人工补「下一步怎么做」。
"""
import ast
import re
import _console  # noqa: E402,F401  # 控制台 UTF-8 守卫（R179）
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "agenthub_mcp.py"

ACTION = re.compile(
    r"(用|请|先|换|改|重试|重新|检查|重跑|改用|传|核对|确认|授予|接入|管理面"
    r"|hub_|cli\.py|onboard|deploy|--agent"
    r"|必填|不能为空|需要|给出"
    r"|只能是|只支持|可选|应为|需为|必须是|范围|如 \d{4}|如[「『\"]|示例|例如"
    r"|你是不是要找|相近|_example_hint)")
WRAPPER = re.compile(
    r"^错误：\s*(?:\{[^{}]+\}\s*)+[。.]?"
    r"(?:\s*(?:警告|已导出|已备份|已|$).*)?$")


def literal_text(node) -> str:
    """把字符串表达式还原成人读文本：常量原样、f-string 保留 {占位符}。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(literal_text(v) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return "{" + ast.unparse(node.value) + "}"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return literal_text(node.left) + literal_text(node.right)
    if isinstance(node, ast.IfExp):
        return literal_text(node.body) + " " + literal_text(node.orelse)
    return ""


def extract_errors(source: str) -> list[str]:
    tree = ast.parse(source)
    seen, out = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Return):
            value = node.value
        elif isinstance(node, ast.Raise):
            value = node.exc
        else:
            continue
        msg = literal_text(value).strip() if value is not None else ""
        if ("错误：" not in msg and "失败：" not in msg) or msg in seen:
            continue
        seen.add(msg)
        out.append(msg)
    return out


def classify(messages: list[str]) -> tuple[list[str], list[str]]:
    candidates, wrappers = [], []
    for msg in messages:
        if WRAPPER.match(msg) or msg.strip() in ("错误：", "失败："):
            wrappers.append(msg)
        elif not ACTION.search(msg):
            candidates.append(msg)
    return candidates, wrappers


def main() -> None:
    msgs = extract_errors(SRC.read_text(encoding="utf-8"))
    candidates, wrappers = classify(msgs)
    print(f"错误文案 {len(msgs)} 条（去重后），其中 {len(candidates)} 条无动作提示（候选审查）：")
    for msg in candidates:
        print("  -", msg[:140].replace("\n", " "))
    if wrappers:
        print(f"另 {len(wrappers)} 条纯透传包装（动作在内层错误里，不计数）。")


if __name__ == "__main__":
    main()
