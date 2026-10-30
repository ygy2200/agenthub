# -*- coding: utf-8 -*-
"""术语审计守卫（R787）：tools/jargon_audit.py 必须 0 命中。

R787 首跑抓出 14 处，实体修复 7 处（概览页工具名、流水页工具名、搜索页工具名、
设置页 WCAG/jsonl、流程页 MCP、统计页 P90），其余是给 agent 的剪贴板提示词与
接入/资产页必须保留的协议名——口径收在工具里，这里只钉「不许回潮」。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_jargon_audit_is_clean():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "jargon_audit.py")],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout + r.stderr


def test_jargon_terms_can_bite():
    sys.path.insert(0, str(ROOT / "tools"))
    import jargon_audit

    bad = ["最近会话（hub_heartbeat 上报）", "CLI：tasks --stats", "写进 journal 表",
           "SQL 索引", "P90 4h", "WCAG 验算", "token 过期", "MCP 工具", "虚拟滚动模式"]
    hits = [t for t in bad if any(rx.search(t) for rx, _why, _allow in jargon_audit.TERMS)]
    assert len(hits) == len(bad), f"术语表失去咬合力：{set(bad) - set(hits)}"
