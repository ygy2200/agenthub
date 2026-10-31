# -*- coding: utf-8 -*-
"""会话简报入口（R811）：SessionStart 钩子的 Python 侧，与 hub_brief 共用 brain.session_brief。

为什么单独成文件：钩子每次会话启动都要跑，越少 import 越稳、越快；本脚本只依赖
core/brain，任何异常都静默退出 0——钩子绝不能因为大脑暂时不可读而拦住会话。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # Windows 管道默认 cp936，简报里的 ▸/… 会 UnicodeEncodeError 后静默变空
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass
import brain  # noqa: E402
import core  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AgentHub 会话简报（SessionStart 钩子入口）")
    ap.add_argument("--root", default="", help="大脑根目录；默认读 ~/.agenthub/config.json")
    ap.add_argument("--agent", default="", help="会话 agent 名")
    ap.add_argument("--cwd", default="", help="工作目录（用于推断项目）")
    ap.add_argument("--project", default="", help="显式项目（优先于 cwd 推断）")
    ap.add_argument("--max-chars", type=int, default=0, help="字符预算，默认 3600")
    ap.add_argument("--json", action="store_true", help="输出 JSON（测试/脚本用）")
    ap.add_argument("--no-journal", action="store_true", help="不写 brain_brief 流水（测试用）")
    args = ap.parse_args(argv)
    try:
        root = (args.root or (core.load_config() or {}).get("root", "") or "").strip()
        agent = (args.agent or os.environ.get("AGENTHUB_AGENT", "") or "").strip()
        cwd = args.cwd or os.getcwd()
        kw = {"max_chars": args.max_chars} if args.max_chars > 0 else {}
        res = brain.session_brief(root, agent=agent, cwd=cwd, project=args.project, **kw)
        if not res["text"]:
            return 0
        if root and not args.no_journal:
            try:
                core.journal(root, agent or "unknown", "brain_brief", "session",
                             note=f"{len(res['text'])} chars · project={res['project'] or '-'}")
            except Exception:  # noqa: BLE001 流水失败不影响简报
                pass
        if args.json:
            print(json.dumps({"ok": True, **res}, ensure_ascii=False))
        else:
            print(res["text"])
    except Exception:  # noqa: BLE001 钩子必须 fail-open
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
