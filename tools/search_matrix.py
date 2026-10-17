# -*- coding: utf-8 -*-
"""R4 搜索查询矩阵实测：中文单字/双字/长串/中英混排/路径/标识符/零命中/近义。

一次性诊断脚本（2026-10-17 R4）。用法：python search_matrix.py
判定：命中数 + 首条相关性 + 时延。零命中且相关词存在 = 真盲区。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import brain  # noqa: E402

ROOT = r"D:\agent资料\zcode项目记录"
CASES = [
    ("单字", "坑"),
    ("双字", "卡死"),
    ("长串", "多agent文件统一管理"),
    ("中英混排", "MCP 工具设计"),
    ("路径片段", "agenthub_mcp.py"),
    ("英文标识符", "hub_ops_run"),
    ("零命中", "不存在的词qwerty"),
    ("相关词", "爆显存"),
    ("大写标识符", "HUB_OPS_RUN"),
    ("带空格组合", "CS2 卡死"),
    ("近义改写", "关掉错误"),
    ("数字编号", "3262"),
    ("记忆库-踩坑", "静默腐烂"),
    ("记忆库-偏好", "汇报 三段"),
]


def main():
    print("%-12s%-24s%5s %-44s%6s" % ("类型", "查询", "命中", "首条", "ms"))
    for label, q in CASES:
        t0 = time.perf_counter()
        rs = brain.search_records(ROOT, q, limit=5)
        ms = (time.perf_counter() - t0) * 1000
        if rs:
            first = (rs[0].get("title") or rs[0].get("content", ""))[:40]
        else:
            first = "—"
        print("%-12s%-24s%5d %-44s%6.0f" % (label, q, len(rs), first, ms))
    # 记忆库矩阵
    print()
    for label, q in [("mem-踩坑", "静默腐烂"), ("mem-偏好", "汇报 三段"), ("mem-项目", "agenthub")]:
        t0 = time.perf_counter()
        rs = brain.search_memories(ROOT, q, limit=3)
        ms = (time.perf_counter() - t0) * 1000
        first = (rs[0].get("content", "")[:40] if rs else "—")
        print("%-12s%-24s%5d %-44s%6.0f" % (label, q, len(rs), first, ms))


if __name__ == "__main__":
    main()
