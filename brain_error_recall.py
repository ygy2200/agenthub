# -*- coding: utf-8 -*-
"""报错即时回忆入口（R833）：PostToolUseFailure 钩子的 Python 侧。

与 brain_recall.py 同一纪律：只依赖 core/brain，任何异常静默退出 0，绝不因为大脑
暂时不可读而拦住 agent 的这一轮。区别是它按**工具失败的报错正文**检索，只在真命中
时输出——每次失败都注一段「没找到相关记忆」是纯噪声，还会把模型带偏。

    python brain_error_recall.py --tool Bash --error "ModuleNotFoundError: No module named 'x'"
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
    ap = argparse.ArgumentParser(
        description="AgentHub 报错即时回忆（PostToolUseFailure 钩子入口）")
    ap.add_argument("--root", default="", help="大脑根目录；默认读 ~/.agenthub/config.json")
    ap.add_argument("--agent", default="", help="会话 agent 名")
    ap.add_argument("--cwd", default="", help="工作目录（用于推断项目）")
    ap.add_argument("--project", default="", help="显式项目（优先于 cwd 推断）")
    ap.add_argument("--tool", default="", help="失败的工具名（hub_*/agenthub 自身会跳过，防自喂）")
    ap.add_argument("--error", default="", help="报错正文（钩子从事件里取到的文本）")
    ap.add_argument("--stdin", action="store_true", help="从 stdin 读报错正文（长文本用）")
    ap.add_argument("--session", default="", help="会话 id（同会话同签名只说一次）")
    ap.add_argument("--max-chars", type=int, default=0, help="字符预算，默认 700")
    ap.add_argument("--state", default="", help="去重状态文件")
    ap.add_argument("--json", action="store_true", help="输出 JSON（测试/脚本用）")
    ap.add_argument("--no-journal", action="store_true", help="不写流水（测试用）")
    ap.add_argument("--no-state", action="store_true", help="不读写去重状态（测试用）")
    ap.add_argument("--no-count", action="store_true",
                    help="只看不记账：不涨 inject_count、不写采纳账本（冒烟/调参用，"
                         "钩子真送达时不要用——曝光账本是假的，R830 断路器会据此冷却真记忆）")
    ap.add_argument("--event", default="", help="钩子事件名（采集面按事件分账，B067）")
    ap.add_argument("--sample-only", action="store_true",
                    help="不落召回、只落一枚失败样本（B067：钩子拿到了失败事件"
                         "却抽不出报错正文时走这条，否则那 16 次无签名触发永远说不清是什么）")
    ap.add_argument("--shape", default="", help="显式形状标签（钩子侧已知事件形态时用）")
    args = ap.parse_args(argv)
    try:
        root = (args.root or (core.load_config() or {}).get("root", "") or "").strip()
        agent = (args.agent or os.environ.get("AGENTHUB_AGENT", "") or "").strip()
        cwd = args.cwd or os.getcwd()
        err = args.error
        if args.stdin:
            try:
                err = (err + "\n" + sys.stdin.read(16000))[-16000:]
            except Exception:  # noqa: BLE001
                pass
        if not err.strip():
            if not args.sample_only:
                return 0
        if args.sample_only:
            # 采集轨道：不查库、不注任何东西，只留一枚脱敏样本供巡检归因（B067）
            if root:
                try:
                    brain.sample_failure(root, agent=agent, tool=args.tool,
                                         event=args.event, shape=args.shape,
                                         payload=err, session=args.session)
                except Exception:  # noqa: BLE001 采集失败不影响会话
                    pass
            return 0
        kw = {"max_chars": args.max_chars} if args.max_chars > 0 else {}
        res = brain.error_recall(
            root, err, tool=args.tool, agent=agent, cwd=cwd, project=args.project,
            session=args.session, state_path=args.state,
            update_state=not args.no_state, count=not args.no_count,
            event=args.event, **kw)
        if not res["text"]:
            return 0
        if root and not args.no_journal:
            # 两条路分开记账（R834）：真命中 = brain_error_recall（送达率、采纳回填都吃它），
            # 欠账提示 = brain_error_debt_hint。混成一类会把「大脑没答案」算成「大脑答对了」，
            # 而这两个数恰恰是相反的结论。
            debt_hint = res.get("skipped") == "debt_hint"
            note = (f"{len(res['text'])} chars · tool={args.tool or '?'} "
                    f"· sig={res['sig'][:80]}")
            if debt_hint:
                note = (f"欠账提示 · {note} · open={res.get('debt_open')}")
            else:
                note = (f"{note} · mem={res['memory_ids']} · rec={res['record_ids']}"
                        f" · err={res['error_ids']}")
            try:
                core.journal(
                    root, agent or "unknown",
                    "brain_error_debt_hint" if debt_hint else "brain_error_recall",
                    "debt_hint" if debt_hint else "tool_failure", note=note)
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
