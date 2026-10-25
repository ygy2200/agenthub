# -*- coding: utf-8 -*-
"""R115 临时审计：MCP 层错误文案的可行动性（错误 = 发生了什么 + 怎么修）。"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "agenthub_mcp.py"
t = SRC.read_text(encoding="utf-8")

# 抓「错误：/失败：」开头的字面量（含 f-string 前缀行）
pat = re.compile(r'(?:return|ValueError\(|f")((?:错误|失败)：[^"\n]{10,220})', re.S)
errs = [m.group(1).strip() for m in pat.finditer(t)]
action = re.compile(r"(用|请|先|换|改|重试|重新|检查|重跑|改用|传|核对|确认|hub_|cli\.py|onboard|deploy)")
no_action = [e for e in errs if not action.search(e)]
print(f"错误文案 {len(errs)} 条，其中 {len(no_action)} 条无动作词（候选审查）：")
for e in no_action:
    print("  -", e[:110].replace("\n", " "))
