# -*- coding: utf-8 -*-
"""即时回忆入口（R820）：UserPromptSubmit 钩子的 Python 侧。

与 brain_brief.py 同一纪律：只依赖 core/brain，任何异常静默退出 0，
绝不因为大脑暂时不可读而拦住用户这句话。区别是它按**本次 prompt** 检索，
只在真命中时输出——每句都注入「无相关记忆」只是噪声。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass
import brain  # noqa: E402
import core  # noqa: E402

core.JOURNAL_SINK = brain.journal_add


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AgentHub 即时回忆（UserPromptSubmit 钩子入口）")
    ap.add_argument("--root", default="", help="大脑根目录；默认读 ~/.agenthub/config.json")
    ap.add_argument("--agent", default="", help="会话 agent 名")
    ap.add_argument("--cwd", default="", help="工作目录（用于推断项目）")
    ap.add_argument("--project", default="", help="显式项目（优先于 cwd 推断）")
    ap.add_argument("--prompt", default="", help="用户这句话")
    ap.add_argument("--session", default="", help="会话 id（用于同会话去重）")
    ap.add_argument("--max-chars", type=int, default=0, help="字符预算，默认 900")
    ap.add_argument("--state", default="", help="去重状态文件；默认 ~/.workbuddy/state/brain-recall.json")
    ap.add_argument("--json", action="store_true", help="输出 JSON（测试/脚本用）")
    ap.add_argument("--no-journal", action="store_true", help="不写 brain_recall 流水（测试用）")
    ap.add_argument("--no-state", action="store_true", help="不读写去重状态（测试用）")
    args = ap.parse_args(argv)
    try:
        root = (args.root or (core.load_config() or {}).get("root", "") or "").strip()
        agent = (args.agent or os.environ.get("AGENTHUB_AGENT", "") or "").strip()
        cwd = args.cwd or os.getcwd()
        kw = {"max_chars": args.max_chars} if args.max_chars > 0 else {}
        res = brain.prompt_recall(
            root, args.prompt, agent=agent, cwd=cwd, project=args.project,
            session=args.session, state_path=args.state,
            update_state=not args.no_state, **kw)
        if not res["text"]:
            return 0
        if root and not args.no_journal:
            try:
                core.journal(
                    root, agent or "unknown", "brain_recall", "prompt",
                    note=(f"{len(res['text'])} chars · mem={res['memory_ids']} "
                          f"· terms={' '.join(res['terms'])[:120]}"))
            except Exception:  # noqa: BLE001 流水失败不影响召回
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
